<?php

use App\Models\RuntimeContext;
use App\Models\Server;
use App\Services\RuntimeCapabilityProbe;
use Illuminate\Foundation\Testing\RefreshDatabase;

uses(RefreshDatabase::class);

function loginForRuntimeCapabilities(mixed $page): mixed
{
    return $page->fill('email', 'test@example.com')->fill('password', 'password')
        ->click('Login')->assertSee('Dashboard');
}

it('shows persisted identity findings after probing from server settings', function () {
    seedBrowserResourceStack();
    config(['app.maintenance.store' => 'array']);
    $server = Server::factory()->create([
        'team_id' => 0, 'name' => 'Rootless fixture', 'user' => 'workload', 'port' => 22,
    ]);
    $this->mock(RuntimeCapabilityProbe::class)->shouldReceive('inspect')->once()->andReturn([
        'schema_version' => 1, 'runtime' => 'podman', 'uid' => 1001, 'gid' => 1001,
        'manager' => 'user', 'storage' => ['graph_root' => '/home/workload/storage', 'driver' => 'overlay'],
        'versions' => ['podman' => '5.4.2'],
        'capabilities' => ['backup.restic' => ['available' => false, 'remediation' => 'Install Restic for the backup identity.']],
    ]);
    $page = visit('/login');
    loginForRuntimeCapabilities($page)
        ->navigate('/server/'.$server->uuid)
        ->assertSee('Runtime capabilities')
        ->click('Probe capabilities')
        ->assertSee('UID 1001')
        ->assertSee('Install Restic for the backup identity.')
        ->assertSee('No operations recorded for this runtime identity.')
        ->click('Restricted node executor')
        ->assertSee('Verified Ed25519 host public key')
        ->screenshot(filename: 'runtime-capabilities-probed');
    expect(RuntimeContext::query()->where('server_id', $server->id)->count())->toBe(1);
});
