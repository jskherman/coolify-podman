<?php

use App\Actions\Server\ConfigureNodeExecutor;
use App\Actions\Server\ProbeRuntimeCapabilities;
use App\Actions\Server\SubmitNodeOperation;
use App\Jobs\ExecuteNodeOperationJob;
use App\Models\InstanceSettings;
use App\Models\NodeOperation;
use App\Models\PrivateKey;
use App\Models\Server;
use App\Models\Team;
use App\Models\User;
use App\Services\NodeExecutorTransport;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Queue;
use Illuminate\Support\Str;

uses(RefreshDatabase::class);

beforeEach(function () {
    if (getenv('COOLIFY_RUNTIME_VM_TEST') !== '1') {
        $this->markTestSkipped('Requires the verified disposable native activation fixture.');
    }
});

test('control plane adopts real boot compensation and recovers its immutable release without data rollback', function () {
    Server::flushIdentityMap();
    config(['constants.ssh.mux_enabled' => false, 'cache.default' => 'array', 'app.maintenance.store' => 'array']);
    Queue::fake();
    InstanceSettings::forceCreate(['id' => 0]);
    $directory = storage_path('app/runtime-test-vm');
    $fixture = json_decode(file_get_contents($directory.'/state.json'), true, flags: JSON_THROW_ON_ERROR);
    $activation = json_decode(file_get_contents($directory.'/node-boot-selection-gate.json'), true, flags: JSON_THROW_ON_ERROR);
    expect($activation['state'])->toBe('verified')
        ->and($activation['executor_hash'])->toBe(hash_file('sha256', base_path('scripts/coolify-node-executor.py')))
        ->and($activation['adapter_hash'])->toBe(hash_file('sha256', base_path('scripts/coolify-node-native.py')));
    $team = Team::factory()->create();
    session(['currentTeam' => $team]);
    $actor = User::factory()->create();
    $team->members()->attach($actor, ['role' => 'owner']);
    $broad = PrivateKey::factory()->create(['team_id' => $team->id, 'private_key' => file_get_contents($directory.'/id_ed25519')]);
    $restricted = PrivateKey::factory()->create(['team_id' => $team->id, 'private_key' => file_get_contents($directory.'/node_recovery_ed25519')]);
    $server = Server::factory()->create(['team_id' => $team->id, 'private_key_id' => $broad->id,
        'ip' => '127.0.0.1', 'port' => $fixture['ssh_port'], 'user' => 'workload']);
    expect(instant_remote_process(['cat /etc/coolify-disposable-fixture'], $server, no_sudo: true))->toBe($fixture['operation_id']);
    $context = ProbeRuntimeCapabilities::run($actor, $server);
    $hostLine = collect(file($directory.'/known_hosts'))->first(fn ($line) => str_contains($line, ' ssh-ed25519 '));
    $hostKey = implode(' ', array_slice(explode(' ', trim($hostLine)), 1, 2));
    $policyVersion = $activation['policy_version'];
    $context = ConfigureNodeExecutor::run($actor, $context, ['private_key_uuid' => $restricted->uuid,
        'ssh_host_key' => $hostKey, 'controller_epoch' => 2, 'policy_version' => $policyVersion]);
    $transport = app(NodeExecutorTransport::class);
    $recordPath = $directory.'/node-boot-controller-gate.json';
    $record = file_exists($recordPath)
        ? json_decode(file_get_contents($recordPath), true, flags: JSON_THROW_ON_ERROR)
        : ['fixture' => $fixture['operation_id'], 'policy_version' => $policyVersion, 'operations' => []];
    expect($record['fixture'])->toBe($fixture['operation_id'])->and($record['policy_version'])->toBe($policyVersion);
    $save = function () use (&$record, $recordPath): void {
        $temporary = $recordPath.'.tmp';
        $stream = fopen($temporary, 'w');
        chmod($temporary, 0600);
        fwrite($stream, json_encode($record, JSON_THROW_ON_ERROR | JSON_PRETTY_PRINT)."\n");
        fflush($stream);
        fsync($stream);
        fclose($stream);
        rename($temporary, $recordPath);
    };
    $adopt = function (array $request) use ($context, $actor): NodeOperation {
        return NodeOperation::query()->create([
            'uuid' => $request['operation_id'], 'runtime_context_id' => $context->id, 'actor_id' => $actor->id,
            'resource_id' => $request['resource_id'], 'idempotency_key' => $request['idempotency_key'],
            'action' => $request['action'], 'expected_generation' => $request['expected_generation'],
            'target_operation_id' => $request['target_operation_id'] ?? null, 'cursor' => $request['cursor'] ?? null,
            'transport_fingerprint' => $context->transportFingerprint(), 'request' => $request,
        ]);
    };
    foreach (['activate_unhealthy', 'activate_writer'] as $label) {
        $entry = $activation['operations'][$label];
        $operation = $adopt($entry['request']);
        $job = new ExecuteNodeOperationJob($operation->id, $actor->id);
        $job->handle($transport);
        expect($operation->fresh()->status)->toBe('failed')
            ->and($operation->fresh()->result)->toBe($entry['last_observed'])
            ->and($operation->fresh()->result['release']['data_recovery'])->toBeFalse()
            ->and($operation->fresh()->request)->toBe($entry['request']);
        $job->handle($transport);
        expect($operation->fresh()->attempts)->toBe(1);
    }
    $target = NodeOperation::query()->where('uuid', $activation['operations']['activate_writer']['request']['operation_id'])->firstOrFail();
    $target->update(['status' => 'needs_intervention', 'result' => null]);
    if (isset($record['operations']['recover_writer'])) {
        $recover = $adopt($record['operations']['recover_writer']['request']);
    } else {
        $recover = SubmitNodeOperation::run($actor, $context, [
            'action' => 'recover', 'resource_id' => $activation['resource_id'],
            'expected_generation' => $activation['generation'], 'target_operation_id' => $target->uuid,
            'idempotency_key' => (string) Str::uuid(),
        ]);
        $record['operations']['recover_writer'] = ['request' => $recover->request];
        $save();
    }
    (new ExecuteNodeOperationJob($recover->id, $actor->id))->handle($transport);
    expect($recover->fresh()->status)->toBe('succeeded')
        ->and($recover->fresh()->result['recovered_operation'])->toBe($activation['recovery'])
        ->and($target->fresh()->status)->toBe('failed')
        ->and($target->fresh()->request)->toBe($activation['operations']['activate_writer']['request']);
    $record['operations']['recover_writer']['outcome'] = $recover->fresh()->result;
    $save();
    $cursor = 0;
    for ($page = 0; $page < 20; $page++) {
        $label = 'events_'.$page;
        if (isset($record['operations'][$label])) {
            $events = $adopt($record['operations'][$label]['request']);
        } else {
            $events = SubmitNodeOperation::run($actor, $context, [
                'action' => 'events', 'resource_id' => $activation['resource_id'],
                'expected_generation' => $activation['generation'], 'cursor' => $cursor,
                'idempotency_key' => (string) Str::uuid(),
            ]);
            $record['operations'][$label] = ['request' => $events->request];
            $save();
        }
        (new ExecuteNodeOperationJob($events->id, $actor->id))->handle($transport);
        $events->refresh();
        expect($events->status)->toBe('succeeded');
        $cursor = $events->result['next_cursor'];
        $record['operations'][$label]['outcome'] = $events->result;
        $save();
        if (count($events->result['events']) < 25) {
            break;
        }
    }
    expect($page)->toBeLessThan(20)
        ->and($context->fresh()->event_cursors[$activation['resource_id']])->toBe($cursor)
        ->and($target->fresh()->status)->toBe('failed')
        ->and($target->fresh()->result)->toBe($activation['recovery']);
    $record['state'] = 'verified';
    $save();
})->group('runtime-vm');
