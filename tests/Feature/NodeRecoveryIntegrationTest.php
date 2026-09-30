<?php

use App\Actions\Server\ConfigureNodeExecutor;
use App\Actions\Server\ProbeRuntimeCapabilities;
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

uses(RefreshDatabase::class);

beforeEach(function () {
    if (getenv('COOLIFY_RUNTIME_VM_TEST') !== '1') {
        $this->markTestSkipped('Requires the disposable ownership-handoff fixture.');
    }
});

test('restored controller records a handed-off node outcome without replaying the interrupted restart', function () {
    Server::flushIdentityMap();
    config(['constants.ssh.mux_enabled' => false, 'cache.default' => 'array', 'app.maintenance.store' => 'array']);
    Queue::fake();
    InstanceSettings::forceCreate(['id' => 0]);
    $directory = storage_path('app/runtime-test-vm');
    $fixture = json_decode(file_get_contents($directory.'/state.json'), true, flags: JSON_THROW_ON_ERROR);
    $handoff = json_decode(file_get_contents($directory.'/node-handoff-gate.json'), true, flags: JSON_THROW_ON_ERROR);
    expect($handoff['state'])->toBe('recovered');
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
    $context = ConfigureNodeExecutor::run($actor, $context, ['private_key_uuid' => $restricted->uuid,
        'ssh_host_key' => $hostKey, 'controller_epoch' => 2, 'policy_version' => 3]);
    $operations = [];
    foreach (['request', 'recovery'] as $name) {
        $request = $handoff[$name];
        $operations[$name] = NodeOperation::query()->create([
            'uuid' => $request['operation_id'], 'runtime_context_id' => $context->id, 'actor_id' => $actor->id,
            'resource_id' => $request['resource_id'], 'idempotency_key' => $request['idempotency_key'],
            'target_operation_id' => $request['target_operation_id'] ?? null,
            'action' => $request['action'], 'expected_generation' => $request['expected_generation'],
            'transport_fingerprint' => $context->transportFingerprint(), 'request' => $request,
            'status' => 'uncertain', 'attempts' => 1,
        ]);
    }
    $original = $operations['request']->request;
    (new ExecuteNodeOperationJob($operations['recovery']->id, $actor->id))->handle(app(NodeExecutorTransport::class));
    expect($operations['recovery']->fresh()->status)->toBe('succeeded')
        ->and($operations['recovery']->fresh()->result)->toBe($handoff['outcome'])
        ->and($operations['request']->fresh()->status)->toBe('succeeded')
        ->and($operations['request']->fresh()->request)->toBe($original)
        ->and($operations['request']->fresh()->result['generation'])->toBe(6);
})->group('runtime-vm');
