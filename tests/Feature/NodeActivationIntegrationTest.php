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

test('control plane adopts real native no-op compensation and replayed operation events', function () {
    Server::flushIdentityMap();
    config(['constants.ssh.mux_enabled' => false, 'cache.default' => 'array', 'app.maintenance.store' => 'array']);
    Queue::fake();
    InstanceSettings::forceCreate(['id' => 0]);
    $directory = storage_path('app/runtime-test-vm');
    $fixture = json_decode(file_get_contents($directory.'/state.json'), true, flags: JSON_THROW_ON_ERROR);
    $activation = json_decode(file_get_contents($directory.'/node-activation-gate.json'), true, flags: JSON_THROW_ON_ERROR);
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
    $policyVersion = $activation['policy_version'] ?? 7;
    $context = ConfigureNodeExecutor::run($actor, $context, ['private_key_uuid' => $restricted->uuid,
        'ssh_host_key' => $hostKey, 'controller_epoch' => 2, 'policy_version' => $policyVersion]);
    $transport = app(NodeExecutorTransport::class);
    foreach (['healthy_noop' => 'succeeded', 'activate_unhealthy' => 'failed'] as $label => $status) {
        $entry = $activation['operations'][$label];
        $request = $entry['request'];
        expect($request['policy_version'])->toBe($policyVersion);
        $operation = NodeOperation::query()->create([
            'uuid' => $request['operation_id'], 'runtime_context_id' => $context->id, 'actor_id' => $actor->id,
            'resource_id' => $request['resource_id'], 'idempotency_key' => $request['idempotency_key'],
            'action' => $request['action'], 'expected_generation' => $request['expected_generation'],
            'transport_fingerprint' => $context->transportFingerprint(), 'request' => $request,
        ]);
        $job = new ExecuteNodeOperationJob($operation->id, $actor->id);
        $job->handle($transport);
        expect($operation->fresh()->status)->toBe($status)
            ->and($operation->fresh()->result)->toBe($entry['last_observed'])
            ->and($operation->fresh()->request)->toBe($request)
            ->and($operation->fresh()->isTerminal())->toBeTrue();
        $job->handle($transport);
        expect($operation->fresh()->attempts)->toBe(1);
    }
    $manifestPath = $directory.'/node-activation-controller-gate.json';
    if (file_exists($manifestPath)) {
        $record = json_decode(file_get_contents($manifestPath), true, flags: JSON_THROW_ON_ERROR);
        expect($record['fixture'])->toBe($fixture['operation_id'])->and($record['policy_version'])->toBe($policyVersion);
        $request = $record['events_request'];
        $events = NodeOperation::query()->create([
            'uuid' => $request['operation_id'], 'runtime_context_id' => $context->id, 'actor_id' => $actor->id,
            'resource_id' => $request['resource_id'], 'idempotency_key' => $request['idempotency_key'],
            'action' => 'events', 'cursor' => $request['cursor'], 'expected_generation' => $request['expected_generation'],
            'transport_fingerprint' => $context->transportFingerprint(), 'request' => $request,
        ]);
    } else {
        $events = SubmitNodeOperation::run($actor, $context, ['action' => 'events', 'resource_id' => $activation['resource_id'],
            'expected_generation' => 2, 'idempotency_key' => (string) Str::uuid(), 'cursor' => 0]);
        $record = ['fixture' => $fixture['operation_id'], 'policy_version' => $policyVersion, 'events_request' => $events->request];
        $temporary = $manifestPath.'.tmp';
        file_put_contents($temporary, json_encode($record, JSON_THROW_ON_ERROR | JSON_PRETTY_PRINT)."\n");
        rename($temporary, $manifestPath);
    }
    (new ExecuteNodeOperationJob($events->id, $actor->id))->handle($transport);
    $events->refresh();
    expect($events->status)->toBe('succeeded')->and($events->result['events'])->not->toBeEmpty()
        ->and($context->fresh()->event_cursors[$activation['resource_id']])->toBe($events->result['next_cursor']);
    $compensated = NodeOperation::query()->where('uuid', $activation['operations']['activate_unhealthy']['request']['operation_id'])->firstOrFail();
    expect($compensated->status)->toBe('failed')->and($compensated->result['release']['data_recovery'])->toBeFalse();
    $record['state'] = 'verified';
    $record['events_outcome'] = $events->result;
    file_put_contents($manifestPath, json_encode($record, JSON_THROW_ON_ERROR | JSON_PRETTY_PRINT)."\n");
})->group('runtime-vm');
