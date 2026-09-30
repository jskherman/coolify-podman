<?php

namespace App\Contracts;

use App\Models\Server;

interface RuntimeDriver
{
    public function stopApplication(Server $server, int $applicationId, int $timeout): void;
}
