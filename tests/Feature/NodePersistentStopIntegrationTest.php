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

test('control plane persists real native stop start restart and stopped compensation outcomes', function () {
    Server::flushIdentityMap();
    config(['constants.ssh.mux_enabled' => false, 'cache.default' => 'array', 'app.maintenance.store' => 'array']);
    Queue::fake();
    InstanceSettings::forceCreate(['id' => 0]);
    $directory = storage_path('app/runtime-test-vm');
    $fixture = json_decode(file_get_contents($directory.'/state.json'), true, flags: JSON_THROW_ON_ERROR);
    $activation = json_decode(file_get_contents($directory.'/node-persistent-stop-gate.json'), true, flags: JSON_THROW_ON_ERROR);
    expect($activation['state'])->toBe('verified')
        ->and($activation['hashes'][0])->toBe(hash_file('sha256', base_path('scripts/coolify-node-executor.py')))
        ->and($activation['hashes'][1])->toBe(hash_file('sha256', base_path('scripts/coolify-node-native.py')));
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
    $policyVersion = 10;
    $context = ConfigureNodeExecutor::run($actor, $context, ['private_key_uuid' => $restricted->uuid,
        'ssh_host_key' => $hostKey, 'controller_epoch' => 2, 'policy_version' => $policyVersion]);
    $transport = app(NodeExecutorTransport::class);
    foreach (['stop_interrupted', 'start_after_stop', 'restart_interrupted', 'stop_for_deployment', 'activate_bad', 'restore_start'] as $label) {
        $entry = $activation['operations'][$label];
        $request = $entry['request'];
        $operation = NodeOperation::query()->create([
            'uuid' => $request['operation_id'], 'runtime_context_id' => $context->id, 'actor_id' => $actor->id,
            'resource_id' => $request['resource_id'], 'idempotency_key' => $request['idempotency_key'],
            'action' => $request['action'], 'expected_generation' => $request['expected_generation'],
            'transport_fingerprint' => $context->transportFingerprint(), 'request' => $request,
        ]);
        $job = new ExecuteNodeOperationJob($operation->id, $actor->id);
        $job->handle($transport);
        expect($operation->fresh()->status)->toBe($entry['outcome']['status'])
            ->and($operation->fresh()->result)->toBe($entry['outcome'])
            ->and($operation->fresh()->result['observed']['boot_enabled'])->toBe(in_array($label, ['start_after_stop', 'restart_interrupted', 'restore_start'], true))
            ->and($operation->fresh()->request)->toBe($request);
        $job->handle($transport);
        expect($operation->fresh()->attempts)->toBe(1);
    }
    $path = $directory.'/node-persistent-stop-controller-gate.json';
    $record = file_exists($path) ? json_decode(file_get_contents($path), true, flags: JSON_THROW_ON_ERROR)
        : ['fixture' => $fixture['operation_id'], 'generation' => $activation['generation'], 'operations' => []];
    expect($record['fixture'])->toBe($fixture['operation_id'])
        ->and($record['generation'])->toBe($activation['generation']);
    $save = function () use (&$record, $path): void {
        $stream = fopen($path.'.tmp', 'w');
        chmod($path.'.tmp', 0600);
        fwrite($stream, json_encode($record, JSON_THROW_ON_ERROR | JSON_PRETTY_PRINT)."\n");
        fflush($stream);
        fsync($stream);
        fclose($stream);
        rename($path.'.tmp', $path);
    };
    foreach (['stop', 'start'] as $offset => $action) {
        if (isset($record['operations'][$action]['request'])) {
            $request = $record['operations'][$action]['request'];
            $operation = NodeOperation::query()->create([
                'uuid' => $request['operation_id'], 'runtime_context_id' => $context->id, 'actor_id' => $actor->id,
                'resource_id' => $request['resource_id'], 'idempotency_key' => $request['idempotency_key'],
                'action' => $request['action'], 'expected_generation' => $request['expected_generation'],
                'transport_fingerprint' => $context->transportFingerprint(), 'request' => $request,
            ]);
        } else {
            $operation = SubmitNodeOperation::run($actor, $context, [
                'action' => $action, 'resource_id' => $activation['resource_id'],
                'expected_generation' => $record['generation'] + $offset, 'idempotency_key' => (string) Str::uuid(),
            ]);
            $record['operations'][$action] = ['request' => $operation->request, 'observations' => []];
            $save();
        }
        for ($attempt = 0; $attempt < 5; $attempt++) {
            Queue::fake();
            $this->artisan('node:reconcile')->assertSuccessful();
            Queue::assertPushed(ExecuteNodeOperationJob::class, fn ($job) => $job->operationId === $operation->id);
            (new ExecuteNodeOperationJob($operation->id, $actor->id))->handle($transport);
            $operation->refresh();
            $record['operations'][$action]['observations'][] = $operation->result;
            $save();
            if ($operation->isTerminal()) {
                break;
            }
            expect($operation->status)->toBe('needs_intervention')
                ->and($operation->error_code)->toBe('lifecycle_effect_unresolved');
            sleep(2);
        }
        expect($operation->status)->toBe('succeeded')
            ->and($operation->result['generation'])->toBe($record['generation'] + $offset + 1)
            ->and($operation->result['observed']['boot_enabled'])->toBe($action === 'start')
            ->and($operation->result['observed']['active'])->toBe($action === 'start')
            ->and($operation->result['observed']['failed'])->toBeFalse();
        $record['operations'][$action]['outcome'] = $operation->result;
        $save();
    }
    $record['state'] = 'verified';
    $record['final_generation'] = $record['generation'] + 2;
    $save();
})->group('runtime-vm');
