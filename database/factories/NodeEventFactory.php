<?php

namespace Database\Factories;

use App\Models\RuntimeContext;
use Illuminate\Database\Eloquent\Factories\Factory;
use Illuminate\Support\Arr;

class NodeEventFactory extends Factory
{
    public function definition(): array
    {
        $resource = fake()->uuid();
        $operation = fake()->uuid();
        $payload = ['resource_id' => $resource, 'operation_id' => $operation, 'status' => 'succeeded', 'generation' => 1];

        return ['runtime_context_id' => RuntimeContext::factory(), 'resource_id' => $resource,
            'operation_uuid' => $operation, 'sequence' => 1, 'payload' => $payload,
            'payload_hash' => hash('sha256', json_encode(Arr::sortRecursive($payload), JSON_THROW_ON_ERROR))];
    }
}
