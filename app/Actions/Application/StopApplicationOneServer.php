<?php

namespace App\Actions\Application;

use App\Contracts\RuntimeDriver;
use App\Models\Application;
use App\Models\Server;
use Lorisleiva\Actions\Concerns\AsAction;

class StopApplicationOneServer
{
    use AsAction;

    public function __construct(private RuntimeDriver $runtime) {}

    public function handle(Application $application, Server $server): ?string
    {
        if ($application->destination->server->isSwarm()) {
            return null;
        }
        if (! $server->isFunctional()) {
            return 'Server is not functional';
        }
        try {
            $this->runtime->stopApplication($server, $application->id, $application->settings->stopGracePeriodSeconds());
        } catch (\Exception $e) {
            return $e->getMessage();
        }

        return null;
    }
}
