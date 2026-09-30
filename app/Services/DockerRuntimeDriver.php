<?php

namespace App\Services;

use App\Contracts\RuntimeDriver;
use App\Models\Server;

class DockerRuntimeDriver implements RuntimeDriver
{
    public function stopApplication(Server $server, int $applicationId, int $timeout): void
    {
        $containers = getCurrentApplicationContainerStatus($server, $applicationId, 0);

        foreach ($containers as $container) {
            $containerName = data_get($container, 'Names');
            if ($containerName) {
                instant_remote_process([
                    dockerStopCommand($timeout, escapeshellarg($containerName), $server),
                    dockerRemoveCommand($containerName),
                ], $server);
            }
        }
    }
}
