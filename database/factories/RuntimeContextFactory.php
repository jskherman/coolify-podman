<?php

namespace Database\Factories;

use App\Models\Server;
use App\Models\Team;
use Illuminate\Database\Eloquent\Factories\Factory;

class RuntimeContextFactory extends Factory
{
    public function definition(): array
    {
        return [
            'server_id' => Server::factory()->state(['team_id' => Team::factory()]), 'runtime' => 'podman',
            'execution_uid' => 1001, 'execution_gid' => 1001, 'manager' => 'user',
            'identity_hash' => hash('sha256', fake()->uuid()),
            'connection_fingerprint' => hash('sha256', fake()->uuid()),
            'capability_fingerprint' => hash('sha256', fake()->uuid()),
            'storage' => [], 'versions' => [], 'capabilities' => [], 'probed_at' => now(),
        ];
    }
}
