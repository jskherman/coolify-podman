<?php

namespace App\Actions\Server;

use App\Models\RuntimeContext;
use App\Models\Server;
use App\Models\User;
use App\Services\RuntimeCapabilityProbe;
use Illuminate\Support\Facades\Gate;
use Lorisleiva\Actions\Concerns\AsAction;

class ProbeRuntimeCapabilities
{
    use AsAction;

    public function __construct(private RuntimeCapabilityProbe $probe) {}

    public function handle(User $actor, Server $server): RuntimeContext
    {
        $server->refresh();
        Gate::forUser($actor)->authorize('manageRuntime', $server);
        $connection = RuntimeContext::connectionFingerprint($server);
        $report = $this->probe->inspect($server);
        $server->refresh();
        Gate::forUser($actor)->authorize('manageRuntime', $server);
        abort_unless(hash_equals($connection, RuntimeContext::connectionFingerprint($server)), 409, 'Server identity changed during the probe. Probe again.');
        $identity = [$connection, $report['runtime'], $report['uid'], $report['gid'], $report['manager'], $report['storage']];

        return RuntimeContext::query()->updateOrCreate([
            'identity_hash' => hash('sha256', json_encode($identity, JSON_THROW_ON_ERROR)),
        ], [
            'server_id' => $server->id, 'runtime' => $report['runtime'],
            'execution_uid' => $report['uid'], 'execution_gid' => $report['gid'], 'manager' => $report['manager'],
            'connection_fingerprint' => $connection,
            'capability_fingerprint' => hash('sha256', json_encode($report, JSON_THROW_ON_ERROR)),
            'storage' => $report['storage'], 'versions' => $report['versions'],
            'capabilities' => $report['capabilities'], 'probed_at' => now(),
        ]);
    }
}
