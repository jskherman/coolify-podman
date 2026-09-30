<?php

use App\Actions\Application\StopApplicationOneServer;
use App\Actions\Docker\GetContainersStatus;
use App\Contracts\RuntimeDriver;
use App\Livewire\Project\Shared\Destination;
use App\Models\Application;
use App\Models\Environment;
use App\Models\InstanceSettings;
use App\Models\PrivateKey;
use App\Models\Project;
use App\Models\Server;
use App\Models\StandaloneDocker;
use App\Models\Team;
use App\Models\User;
use App\Services\DockerRuntimeDriver;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Process;
use Livewire\Livewire;

uses(RefreshDatabase::class);

beforeEach(function () {
    $this->withoutVite();
    Server::flushIdentityMap();
    InstanceSettings::forceCreate(['id' => 0]);
    config(['constants.ssh.mux_enabled' => false]);
    $team = Team::factory()->create();
    $this->team = $team;
    $this->server = Server::factory()->create([
        'team_id' => $team->id,
        'user' => 'root',
        'port' => 22,
        'private_key_id' => PrivateKey::factory()->create(['team_id' => $team->id])->id,
    ]);
    $this->server->settings->update([
        'is_reachable' => true,
        'is_usable' => true,
        'force_disabled' => false,
        'docker_version' => '28.0.0',
    ]);
    $destination = StandaloneDocker::query()->where('server_id', $this->server->id)->firstOrFail();
    $project = Project::factory()->create(['team_id' => $team->id]);
    $environment = Environment::factory()->create(['project_id' => $project->id]);
    $this->application = Application::factory()->create([
        'environment_id' => $environment->id,
        'destination_id' => $destination->id,
        'destination_type' => $destination->getMorphClass(),
    ]);
    $this->application->settings->update(['stop_grace_period' => 42]);
    Process::fake();
});

it('resolves the inherited runtime to the Docker adapter', function () {
    expect(app(RuntimeDriver::class))->toBeInstanceOf(DockerRuntimeDriver::class);
});

it('routes a one-server stop through the runtime contract', function () {
    $driver = Mockery::mock(RuntimeDriver::class);
    $driver->shouldReceive('stopApplication')->once()
        ->with($this->server, $this->application->id, 42);
    app()->instance(RuntimeDriver::class, $driver);

    expect(StopApplicationOneServer::run($this->application, $this->server))->toBeNull();
    Process::assertNothingRan();
});

it('does not invoke a driver when the server is unavailable or the application uses swarm', function (array $settings, ?string $result) {
    $this->server->settings->update($settings);
    $this->application->destination->server->settings->refresh();
    $driver = Mockery::mock(RuntimeDriver::class);
    $driver->shouldNotReceive('stopApplication');
    app()->instance(RuntimeDriver::class, $driver);

    expect(StopApplicationOneServer::run($this->application, $this->server))->toBe($result);
    Process::assertNothingRan();
})->with([
    'unavailable' => [['is_usable' => false], 'Server is not functional'],
    'swarm' => [['is_swarm_manager' => true], null],
]);

it('returns a driver failure without attempting another runtime', function () {
    $driver = Mockery::mock(RuntimeDriver::class);
    $driver->shouldReceive('stopApplication')->once()->andThrow(new RuntimeException('Runtime unavailable'));
    app()->instance(RuntimeDriver::class, $driver);

    expect(StopApplicationOneServer::run($this->application, $this->server))->toBe('Runtime unavailable');
    Process::assertNothingRan();
});

it('stops only base containers using the configured timeout and engine version', function (string $version, string $flag) {
    $this->server->settings->update(['docker_version' => $version]);
    $applicationId = $this->application->id;
    Process::fake(function ($process) use ($applicationId) {
        if (str_contains($process->command, 'docker ps -a')) {
            return Process::result(output: collect([
                ['Names' => 'base-legacy', 'Labels' => "coolify.applicationId={$applicationId}"],
                ['Names' => 'base-zero', 'Labels' => "coolify.applicationId={$applicationId},coolify.pullRequestId=0"],
                ['Names' => 'preview', 'Labels' => "coolify.applicationId={$applicationId},coolify.pullRequestId=17"],
                ['Names' => '', 'Labels' => "coolify.applicationId={$applicationId}"],
            ])->map(fn (array $container): string => json_encode($container))->implode("\n"));
        }

        return Process::result();
    });

    expect(StopApplicationOneServer::run($this->application, $this->server))->toBeNull();

    foreach (['base-legacy', 'base-zero'] as $name) {
        Process::assertRan(fn ($process): bool => str_contains($process->command, "docker stop {$flag}=42 '{$name}'\n")
            && str_contains($process->command, dockerRemoveCommand($name)));
    }
    Process::assertDidntRun(fn ($process): bool => str_contains($process->command, "'preview'"));
    Process::assertRanTimes(fn ($process): bool => str_contains($process->command, 'docker stop'), 2);
})->with(['current' => ['28.0.0', '--timeout'], 'legacy' => ['27.5.1', '--time']]);

it('does not issue a stop when discovery finds no containers', function () {
    expect(StopApplicationOneServer::run($this->application, $this->server))->toBeNull();
    Process::assertDidntRun(fn ($process): bool => str_contains($process->command, 'docker stop'));
});

it('reports a failed remote stop', function () {
    Process::fake(function ($process) {
        if (str_contains($process->command, 'docker ps -a')) {
            return Process::result(output: json_encode(['Names' => 'base', 'Labels' => 'coolify.pullRequestId=0']));
        }

        return Process::result(errorOutput: 'daemon unavailable', exitCode: 1);
    });

    expect(StopApplicationOneServer::run($this->application, $this->server))->toContain('daemon unavailable');
});

it('allows an owner to stop an application through the existing destination control', function () {
    $user = User::factory()->create();
    $this->team->members()->attach($user->id, ['role' => 'owner']);
    $this->actingAs($user);
    session(['currentTeam' => $this->team]);
    GetContainersStatus::shouldRun()->once();
    $driver = Mockery::mock(RuntimeDriver::class);
    $driver->shouldReceive('stopApplication')->once()->withArgs(fn (Server $server, int $applicationId, int $timeout): bool => $server->is($this->server) && $applicationId === $this->application->id && $timeout === 42
    );
    app()->instance(RuntimeDriver::class, $driver);

    Livewire::test(Destination::class, ['resource' => $this->application])
        ->call('stop', $this->server->id)
        ->assertDispatched('refresh');
    Process::assertNothingRan();
});

it('denies members and foreign-team users before invoking the runtime', function (bool $foreignTeam) {
    $user = User::factory()->create();
    $team = $foreignTeam ? Team::factory()->create() : $this->team;
    $team->members()->attach($user->id, ['role' => $foreignTeam ? 'owner' : 'member']);
    $this->actingAs($user);
    session(['currentTeam' => $team]);
    $driver = Mockery::mock(RuntimeDriver::class);
    $driver->shouldNotReceive('stopApplication');
    app()->instance(RuntimeDriver::class, $driver);

    $component = Livewire::test(Destination::class, ['resource' => $this->application]);
    if ($foreignTeam) {
        $component->assertForbidden();
    } else {
        $component->call('stop', $this->server->id)->assertDispatched('error');
    }
    Process::assertNothingRan();
})->with(['member' => false, 'foreign owner' => true]);

it('rejects a foreign server even when the actor owns the application', function () {
    $user = User::factory()->create();
    $this->team->members()->attach($user->id, ['role' => 'owner']);
    $this->actingAs($user);
    session(['currentTeam' => $this->team]);
    $foreignServer = Server::factory()->create(['team_id' => Team::factory()->create()->id]);
    $driver = Mockery::mock(RuntimeDriver::class);
    $driver->shouldNotReceive('stopApplication');
    app()->instance(RuntimeDriver::class, $driver);

    Livewire::test(Destination::class, ['resource' => $this->application])
        ->call('stop', $foreignServer->id)
        ->assertNotFound();
    Process::assertNothingRan();
});

it('surfaces a failed stop without refreshing or detaching the destination', function (bool $remove) {
    $user = User::factory()->create();
    $this->team->members()->attach($user->id, ['role' => 'owner']);
    $this->actingAs($user);
    session(['currentTeam' => $this->team]);
    $additionalServer = Server::factory()->create(['team_id' => $this->team->id]);
    $network = StandaloneDocker::query()->where('server_id', $additionalServer->id)->firstOrFail();
    $this->application->additional_networks()->attach($network->id, ['server_id' => $additionalServer->id]);
    StopApplicationOneServer::shouldRun()->once()->andReturn('Runtime unavailable');
    GetContainersStatus::shouldNotRun();

    $component = Livewire::test(Destination::class, ['resource' => $this->application]);
    if ($remove) {
        $component->call('removeServer', $network->id, $additionalServer->id, 'password', []);
    } else {
        $component->call('stop', $additionalServer->id);
    }

    $component->assertDispatched('error', 'Runtime unavailable')->assertNotDispatched('refresh');
    expect($this->application->additional_networks()->whereKey($network->id)->exists())->toBeTrue();
    Process::assertNothingRan();
})->with(['stop' => false, 'remove' => true]);
