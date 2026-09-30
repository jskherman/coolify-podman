<?php

use App\Actions\Server\ProbeRuntimeCapabilities;
use App\Models\InstanceSettings;
use App\Models\PrivateKey;
use App\Models\Server;
use App\Models\Team;
use App\Models\User;
use Illuminate\Foundation\Testing\RefreshDatabase;

uses(RefreshDatabase::class);

beforeEach(function () {
    if (getenv('COOLIFY_RUNTIME_VM_TEST') !== '1') {
        $this->markTestSkipped('Requires the disposable runtime-test-vm fixture.');
    }
});

it('observes the actual SSH identity without elevating it', function (string $sshUser, int $uid, bool $rootless) {
    Server::flushIdentityMap();
    config(['constants.ssh.mux_enabled' => false]);
    InstanceSettings::forceCreate(['id' => 0]);
    $directory = storage_path('app/runtime-test-vm');
    $fixture = json_decode(file_get_contents($directory.'/state.json'), true, flags: JSON_THROW_ON_ERROR);
    expect($fixture['stage'])->toBe('running');
    $team = Team::factory()->create();
    $actor = User::factory()->create();
    $team->members()->attach($actor, ['role' => 'owner']);
    $key = PrivateKey::factory()->create(['team_id' => $team->id, 'private_key' => file_get_contents($directory.'/id_ed25519')]);
    $server = Server::factory()->create([
        'team_id' => $team->id, 'private_key_id' => $key->id,
        'ip' => '127.0.0.1', 'port' => $fixture['ssh_port'], 'user' => $sshUser,
    ]);
    expect(instant_remote_process(['cat /etc/coolify-disposable-fixture'], $server, no_sudo: true))->toBe($fixture['operation_id']);
    $context = ProbeRuntimeCapabilities::run($actor, $server);
    expect($context->execution_uid)->toBe($uid)
        ->and($context->capabilities['network.rootless']['available'])->toBe($rootless)
        ->and($context->versions['podman'])->toBe('5.4.2')
        ->and($context->capabilities['systemd.quadlet_generator']['available'])->toBeTrue()
        ->and($context->capabilities['compose.pinned_provider']['available'])->toBeFalse();
    if ($rootless) {
        expect($context->storage['graph_root'])->toStartWith('/home/workload/')
            ->and($context->capabilities['systemd.user_units']['available'])->toBeTrue()
            ->and($context->capabilities['systemd.linger']['available'])->toBeTrue();
    }
})->with([['root', 0, false], ['workload', 1001, true]])->group('runtime-vm');
