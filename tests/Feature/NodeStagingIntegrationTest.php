<?php

use App\Actions\Server\ConfigureNodeExecutor;
use App\Actions\Server\ProbeRuntimeCapabilities;
use App\Data\ApplicationSpec;
use App\Jobs\ExecuteNodeOperationJob;
use App\Models\InstanceSettings;
use App\Models\NodeOperation;
use App\Models\PrivateKey;
use App\Models\Server;
use App\Models\Team;
use App\Models\User;
use App\Services\NodeExecutorTransport;
use App\Services\QuadletCompiler;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Queue;

uses(RefreshDatabase::class);

beforeEach(function () {
    if (getenv('COOLIFY_RUNTIME_VM_TEST') !== '1') {
        $this->markTestSkipped('Requires the disposable native staging fixture.');
    }
});

test('control plane replays a native staged bundle without advancing its generation', function () {
    Server::flushIdentityMap();
    config(['constants.ssh.mux_enabled' => false, 'cache.default' => 'array', 'app.maintenance.store' => 'array']);
    Queue::fake();
    InstanceSettings::forceCreate(['id' => 0]);
    $directory = storage_path('app/runtime-test-vm');
    $fixture = json_decode(file_get_contents($directory.'/state.json'), true, flags: JSON_THROW_ON_ERROR);
    $staging = json_decode(file_get_contents($directory.'/node-staging-gate.json'), true, flags: JSON_THROW_ON_ERROR);
    expect($staging['state'])->toBe('verified')
        ->and($staging['code_hash'])->toBe(hash_file('sha256', base_path('scripts/coolify-node-executor.py')));
    $request = $staging['request'];
    expect($request['protocol'])->toBe(2)->and($request['action'])->toBe('stage')
        ->and($request['resource_id'])->toBe($staging['resource_id'])
        ->and($request['controller_epoch'])->toBe(2)->and($request['policy_version'])->toBe(6)
        ->and($request['expected_generation'])->toBe(0);
    $specification = ApplicationSpec::fromArray($request['spec']);
    expect($request['spec'])->toBe($specification->toArray());
    $files = app(QuadletCompiler::class)->compile($specification, $request['resource_id']);
    $bundleHash = hash('sha256', json_encode($files, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_LINE_TERMINATORS));

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
        'ssh_host_key' => $hostKey, 'controller_epoch' => 2, 'policy_version' => 6]);
    expect($request['execution_uid'])->toBe($context->execution_uid)
        ->and($request['execution_gid'])->toBe($context->execution_gid);
    $operation = NodeOperation::query()->create([
        'uuid' => $request['operation_id'], 'runtime_context_id' => $context->id, 'actor_id' => $actor->id,
        'resource_id' => $request['resource_id'], 'idempotency_key' => $request['idempotency_key'],
        'action' => $request['action'], 'expected_generation' => $request['expected_generation'],
        'transport_fingerprint' => $context->transportFingerprint(), 'request' => $request,
    ]);
    $transport = app(NodeExecutorTransport::class);
    $job = new ExecuteNodeOperationJob($operation->id, $actor->id);
    $job->handle($transport);
    $operation->refresh();
    expect($operation->status)->toBe('succeeded')->and($operation->result)->toBe($staging['outcome'])
        ->and($operation->request)->toBe($request)->and($operation->result['generation'])->toBe(0)
        ->and($operation->result['bundle']['state'])->toBe('staged')
        ->and($operation->result['bundle']['resource_id'])->toBe($request['resource_id'])
        ->and($operation->result['bundle']['compiler_version'])->toBe(QuadletCompiler::VERSION)
        ->and($operation->result['bundle']['spec_hash'])->toBe($specification->hash())
        ->and($operation->result['bundle']['bundle_hash'])->toBe($bundleHash);
    expect($transport->execute($context, $request))->toBe($operation->result);
    $job->handle($transport);
    expect($operation->fresh()->attempts)->toBe(1)->and($operation->fresh()->result)->toBe($staging['outcome']);
})->group('runtime-vm');
