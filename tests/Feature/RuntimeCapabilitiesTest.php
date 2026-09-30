<?php

use App\Actions\Server\ProbeRuntimeCapabilities;
use App\Exceptions\RuntimeCapabilityProbeFailed;
use App\Livewire\Server\RuntimeCapabilities;
use App\Models\InstanceSettings;
use App\Models\RuntimeContext;
use App\Models\Server;
use App\Models\Team;
use App\Models\User;
use App\Services\RuntimeCapabilityProbe;
use Illuminate\Auth\Access\AuthorizationException;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Livewire\Livewire;
use Symfony\Component\HttpKernel\Exception\HttpException;

uses(RefreshDatabase::class);

beforeEach(function () {
    Server::flushIdentityMap();
    config(['app.maintenance.store' => 'array', 'cache.default' => 'array', 'cache.stores.redis.driver' => 'array']);
    InstanceSettings::forceCreate(['id' => 0, 'is_api_enabled' => true]);
    $this->team = Team::factory()->create();
    $this->user = User::factory()->create();
    $this->team->members()->attach($this->user, ['role' => 'owner']);
    session(['currentTeam' => $this->team]);
    $this->server = Server::factory()->create(['team_id' => $this->team->id, 'user' => 'workload']);
    $this->report = [
        'schema_version' => 1, 'runtime' => 'podman', 'uid' => 1001, 'gid' => 1001,
        'manager' => 'user', 'storage' => ['graph_root' => '/home/workload/.local/share/containers/storage', 'driver' => 'overlay'],
        'versions' => ['podman' => '5.4.2', 'systemd' => '257'],
        'capabilities' => ['runtime.podman' => ['available' => true, 'remediation' => null], 'backup.restic' => ['available' => false, 'remediation' => 'Install Restic for the backup identity.']],
    ];
    $this->probe = Mockery::mock(RuntimeCapabilityProbe::class);
    app()->instance(RuntimeCapabilityProbe::class, $this->probe);
});

test('owner probes and persists an identity-specific runtime context', function () {
    $this->probe->shouldReceive('inspect')->twice()->andReturn($this->report);
    $first = ProbeRuntimeCapabilities::run($this->user, $this->server);
    $again = ProbeRuntimeCapabilities::run($this->user, $this->server);
    expect($again->id)->toBe($first->id)->and($first->execution_uid)->toBe(1001)
        ->and($first->capabilities['backup.restic']['available'])->toBeFalse()
        ->and($first->capability_fingerprint)->toHaveLength(64);
    expect(RuntimeContext::count())->toBe(1);
});

test('different storage and uid observations cannot silently replace another runtime identity', function () {
    $this->probe->shouldReceive('inspect')->once()->andReturn($this->report);
    $first = ProbeRuntimeCapabilities::run($this->user, $this->server);
    $other = $this->report;
    $other['uid'] = 1002;
    $other['storage']['graph_root'] = '/home/other/.local/share/containers/storage';
    $this->probe->shouldReceive('inspect')->once()->andReturn($other);
    $second = ProbeRuntimeCapabilities::run($this->user, $this->server);
    expect($first->id)->not->toBe($second->id)->and(RuntimeContext::count())->toBe(2);
});

test('member and foreign team cannot run capability probes', function (string $role) {
    if ($role === 'member') {
        $this->team->members()->updateExistingPivot($this->user->id, ['role' => 'member']);
    } else {
        $this->server->update(['team_id' => Team::factory()->create()->id]);
    }
    $this->probe->shouldNotReceive('inspect');
    ProbeRuntimeCapabilities::run($this->user->fresh(), $this->server);
})->with(['member', 'foreign'])->throws(AuthorizationException::class);

test('authorized API probes and lists findings with remediation', function () {
    $this->probe->shouldReceive('inspect')->once()->andReturn($this->report);
    $token = $this->user->createToken('runtime', ['read', 'write'])->plainTextToken;
    $this->withToken($token)->postJson("/api/v1/servers/{$this->server->uuid}/runtime-contexts/probe")
        ->assertOk()->assertJsonPath('execution_uid', 1001);
    $this->withToken($token)->getJson("/api/v1/servers/{$this->server->uuid}/runtime-contexts")
        ->assertOk()->assertJsonFragment(['available' => false, 'remediation' => 'Install Restic for the backup identity.']);
});

test('API rejects member reads, foreign scope and read-only probe tokens', function () {
    $this->probe->shouldNotReceive('inspect');
    $token = $this->user->createToken('runtime', ['read'])->plainTextToken;
    $this->withToken($token)->postJson("/api/v1/servers/{$this->server->uuid}/runtime-contexts/probe")->assertForbidden();
    $foreign = Server::factory()->create(['team_id' => Team::factory()->create()->id]);
    $this->withToken($token)->getJson("/api/v1/servers/{$foreign->uuid}/runtime-contexts")->assertNotFound();
    $this->team->members()->updateExistingPivot($this->user->id, ['role' => 'member']);
    $this->user->unsetRelation('teams');
    $this->withToken($token)->getJson("/api/v1/servers/{$this->server->uuid}/runtime-contexts")->assertForbidden();
});

test('UI probes and displays missing prerequisites', function () {
    $this->probe->shouldReceive('inspect')->once()->andReturn($this->report);
    Livewire::actingAs($this->user)->test(RuntimeCapabilities::class, ['server' => $this->server])
        ->call('probe')->assertSee('Install Restic for the backup identity.')->assertSee('1001');
});

test('UI rejects unauthorized reads and reauthorizes probe actions', function () {
    $component = Livewire::actingAs($this->user)->test(RuntimeCapabilities::class, ['server' => $this->server]);
    $this->team->members()->updateExistingPivot($this->user->id, ['role' => 'member']);
    $this->user->unsetRelation('teams');
    $this->probe->shouldNotReceive('inspect');
    $component->call('probe')->assertForbidden();
    Livewire::actingAs($this->user->fresh())->test(RuntimeCapabilities::class, ['server' => $this->server])->assertForbidden();
});

test('an identity change during observation refuses persistence', function () {
    $this->probe->shouldReceive('inspect')->once()->andReturnUsing(function () {
        Server::query()->whereKey($this->server->id)->update(['user' => 'another-user']);

        return $this->report;
    });
    try {
        ProbeRuntimeCapabilities::run($this->user, $this->server);
        test()->fail('Expected stale identity rejection.');
    } catch (HttpException $exception) {
        expect($exception->getStatusCode())->toBe(409)->and(RuntimeContext::count())->toBe(0);
    }
});

test('revoking administration during observation refuses persistence', function () {
    $this->probe->shouldReceive('inspect')->once()->andReturnUsing(function () {
        $this->team->members()->updateExistingPivot($this->user->id, ['role' => 'member']);

        return $this->report;
    });
    try {
        ProbeRuntimeCapabilities::run($this->user, $this->server);
        test()->fail('Expected revoked role rejection.');
    } catch (AuthorizationException) {
        expect(RuntimeContext::count())->toBe(0);
    }
});

test('old observations are not presented for a changed SSH identity', function () {
    $this->probe->shouldReceive('inspect')->once()->andReturn($this->report);
    ProbeRuntimeCapabilities::run($this->user, $this->server);
    $this->server->update(['user' => 'another-user']);
    $token = $this->user->createToken('runtime', ['read'])->plainTextToken;
    $this->withToken($token)->getJson("/api/v1/servers/{$this->server->uuid}/runtime-contexts")
        ->assertOk()->assertExactJson([]);
    expect(RuntimeContext::count())->toBe(1);
});

test('probe outages expose remediation and preserve prior observations', function () {
    $this->probe->shouldReceive('inspect')->once()->andReturn($this->report);
    $context = ProbeRuntimeCapabilities::run($this->user, $this->server);
    $this->probe->shouldReceive('inspect')->twice()->andThrow(new RuntimeCapabilityProbeFailed('Verify SSH and Python 3, then retry.'));
    $token = $this->user->createToken('runtime', ['write'])->plainTextToken;
    $this->withToken($token)->postJson("/api/v1/servers/{$this->server->uuid}/runtime-contexts/probe")
        ->assertUnprocessable()->assertJsonPath('code', 'probe_unavailable');
    Livewire::actingAs($this->user)->test(RuntimeCapabilities::class, ['server' => $this->server])
        ->call('probe')->assertDispatched('error')->assertNotDispatched('success');
    expect($context->fresh()->capability_fingerprint)->toBe($context->capability_fingerprint)
        ->and(RuntimeContext::count())->toBe(1);
});
