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
use phpseclib3\Crypt\EC;

uses(RefreshDatabase::class);

beforeEach(function () {
    if (getenv('COOLIFY_RUNTIME_VM_TEST') !== '1') {
        $this->markTestSkipped('Requires the disposable restricted executor fixture after interruption tests.');
    }
});

test('control plane persists and reconciles actual restricted SSH outcomes with pinned identity', function () {
    Server::flushIdentityMap();
    config(['constants.ssh.mux_enabled' => false, 'app.maintenance.store' => 'array', 'cache.default' => 'array']);
    InstanceSettings::forceCreate(['id' => 0]);
    Queue::fake();
    $directory = storage_path('app/runtime-test-vm');
    $fixture = json_decode(file_get_contents($directory.'/state.json'), true, flags: JSON_THROW_ON_ERROR);
    expect($fixture['stage'])->toBe('running')->and($fixture['interruption_operation']['state'])->toBe('reconciled');
    $team = Team::factory()->create();
    session(['currentTeam' => $team]);
    $actor = User::factory()->create();
    $team->members()->attach($actor, ['role' => 'owner']);
    $broad = PrivateKey::factory()->create(['team_id' => $team->id, 'private_key' => file_get_contents($directory.'/id_ed25519')]);
    $restricted = PrivateKey::factory()->create(['team_id' => $team->id, 'private_key' => file_get_contents($directory.'/node_ed25519')]);
    $server = Server::factory()->create(['team_id' => $team->id, 'private_key_id' => $broad->id,
        'ip' => '127.0.0.1', 'port' => $fixture['ssh_port'], 'user' => 'workload']);
    expect(instant_remote_process(['cat /etc/coolify-disposable-fixture'], $server, no_sudo: true))->toBe($fixture['operation_id']);
    $context = ProbeRuntimeCapabilities::run($actor, $server);
    $hostLine = collect(file($directory.'/known_hosts'))->first(fn ($line) => str_contains($line, ' ssh-ed25519 '));
    $hostKey = implode(' ', array_slice(explode(' ', trim($hostLine)), 1, 2));
    $context = ConfigureNodeExecutor::run($actor, $context, ['private_key_uuid' => $restricted->uuid,
        'ssh_host_key' => $hostKey, 'controller_epoch' => 1, 'policy_version' => 2]);
    $manifest = $directory.'/control-plane-operations.json';
    $recorded = file_exists($manifest) ? json_decode(file_get_contents($manifest), true, flags: JSON_THROW_ON_ERROR) : [];
    $transport = app(NodeExecutorTransport::class);
    foreach (['status' => 5, 'start' => 5, 'discovery' => 0] as $action => $generation) {
        if (isset($recorded[$action])) {
            $request = $recorded[$action];
            $operation = NodeOperation::query()->create(['uuid' => $request['operation_id'], 'runtime_context_id' => $context->id,
                'actor_id' => $actor->id, 'idempotency_key' => $request['idempotency_key'], 'resource_id' => $request['resource_id'],
                'action' => $request['action'], 'expected_generation' => $request['expected_generation'],
                'transport_fingerprint' => $context->transportFingerprint(), 'request' => $request]);
        } else {
            $operation = SubmitNodeOperation::run($actor, $context, ['resource_id' => $fixture['executor_fixture']['resource_id'],
                'action' => $action === 'discovery' ? 'status' : $action, 'expected_generation' => $generation, 'idempotency_key' => (string) Str::uuid()]);
            $recorded[$action] = $operation->request;
            file_put_contents($manifest.'.tmp', json_encode($recorded, JSON_PRETTY_PRINT | JSON_THROW_ON_ERROR));
            rename($manifest.'.tmp', $manifest);
        }
        (new ExecuteNodeOperationJob($operation->id, $actor->id))->handle($transport);
        $operation->refresh();
        expect($operation->status)->toBe('succeeded')->and($operation->result['generation'])->toBe(5)
            ->and($operation->result['observed']['active'])->toBeTrue();
        $again = $transport->execute($context, $operation->request);
        expect($again)->toBe($operation->result);
    }
    $context->ssh_host_key = trim(EC::createKey('Ed25519')->getPublicKey()->toString('OpenSSH', ['comment' => '']));
    expect(fn () => $transport->execute($context, $operation->request))->toThrow(RuntimeException::class);
})->group('runtime-vm');
