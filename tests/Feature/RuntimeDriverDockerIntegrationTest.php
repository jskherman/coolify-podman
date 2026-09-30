<?php

use App\Actions\Application\StopApplicationOneServer;
use App\Models\Application;
use App\Models\Environment;
use App\Models\InstanceSettings;
use App\Models\PrivateKey;
use App\Models\Project;
use App\Models\Server;
use App\Models\StandaloneDocker;
use App\Models\Team;
use Illuminate\Foundation\Testing\RefreshDatabase;

uses(RefreshDatabase::class);

beforeEach(function () {
    if (getenv('COOLIFY_RUNTIME_VM_TEST') !== '1') {
        $this->markTestSkipped('Requires scripts/runtime-test-vm and COOLIFY_RUNTIME_VM_TEST=1; this is a mandatory FND-002 runtime gate.');
    }
});

it('stops and removes only base application containers on real Docker', function (string $sshUser) {
    Server::flushIdentityMap();
    config(['constants.ssh.mux_enabled' => false, 'constants.ssh.command_timeout' => 120]);
    InstanceSettings::forceCreate(['id' => 0]);
    $fixtureDirectory = storage_path('app/runtime-test-vm');
    $fixture = json_decode(file_get_contents($fixtureDirectory.'/state.json'), true, flags: JSON_THROW_ON_ERROR);
    $profile = json_decode(file_get_contents(base_path('.dev/runtime-vm.json')), true, flags: JSON_THROW_ON_ERROR);
    expect($fixture['stage'])->toBe('running');
    $team = Team::factory()->create();
    $key = PrivateKey::factory()->create([
        'team_id' => $team->id,
        'private_key' => file_get_contents($fixtureDirectory.'/id_ed25519'),
    ]);
    $server = Server::factory()->create([
        'team_id' => $team->id,
        'private_key_id' => $key->id,
        'ip' => '127.0.0.1',
        'port' => $fixture['ssh_port'],
        'user' => $sshUser,
    ]);

    expect(instant_remote_process(['cat /etc/coolify-disposable-fixture'], $server, no_sudo: true))
        ->toBe($fixture['operation_id']);
    $version = instant_remote_process(["docker version --format '{{.Server.Version}}'"], $server);
    $server->settings->update(['is_reachable' => true, 'is_usable' => true, 'force_disabled' => false, 'docker_version' => $version]);
    $destination = StandaloneDocker::query()->where('server_id', $server->id)->firstOrFail();
    $environment = Environment::factory()->create(['project_id' => Project::factory()->create(['team_id' => $team->id])->id]);
    $application = Application::factory()->create([
        'environment_id' => $environment->id,
        'destination_id' => $destination->id,
        'destination_type' => $destination->getMorphClass(),
    ]);
    $application->settings->update(['stop_grace_period' => 2]);
    expect(instant_remote_process(["docker ps -aq --filter label=coolify.applicationId={$application->id}"], $server))->toBe('');

    $names = collect(['base', 'preview', 'unrelated'])->mapWithKeys(fn (string $role): array => [$role => "fixture-{$application->uuid}-{$role}"]);
    try {
        foreach ($names as $role => $name) {
            $labels = $role === 'unrelated' ? '' : " --label coolify.applicationId={$application->id}";
            if ($role === 'preview') {
                $labels .= ' --label coolify.pullRequestId=17';
            }
            instant_remote_process(['docker run -d --name '.escapeshellarg($name).$labels.' '.escapeshellarg($profile['test_image']).' sleep 600'], $server);
        }

        expect(StopApplicationOneServer::run($application, $server))->toBeNull();
        expect(instant_remote_process(['docker ps -aq --filter name='.escapeshellarg('^'.$names['base'].'$')], $server))->toBe('');
        foreach (['preview', 'unrelated'] as $role) {
            expect(instant_remote_process(["docker inspect --format '{{.State.Running}}' ".escapeshellarg($names[$role])], $server))->toBe('true');
        }
        expect(StopApplicationOneServer::run($application, $server))->toBeNull();
    } finally {
        instant_remote_process($names->map(fn (string $name): string => dockerRemoveCommand($name))->all(), $server);
    }
})->with(['root', 'operator'])->group('runtime-vm');
