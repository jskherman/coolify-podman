<?php

namespace App\Actions\Server;

use App\Models\NodeEvent;
use App\Models\NodeOperation;
use App\Models\RuntimeContext;
use App\Models\User;
use App\Services\NodeActivationValidator;
use App\Services\NodeStagingValidator;
use Illuminate\Support\Arr;
use Illuminate\Support\Facades\Gate;
use Illuminate\Support\Facades\Validator;
use Illuminate\Support\Str;
use Illuminate\Validation\Rule;
use Lorisleiva\Actions\Concerns\AsAction;
use RuntimeException;

class ReceiveNodeEvents
{
    use AsAction;

    public function __construct(private NodeStagingValidator $stagingValidator, private NodeActivationValidator $activationValidator) {}

    /** @param list<array<string, mixed>> $events */
    public function handle(User $actor, RuntimeContext $context, string $resourceId, array $events): void
    {
        Gate::forUser($actor)->authorize('manageRuntime', $context->server);
        Validator::make(['resource_id' => $resourceId, 'events' => $events], [
            'resource_id' => ['required', 'uuid'], 'events' => ['present', 'array', 'max:50'],
            'events.*.sequence' => ['required', 'integer', 'min:1'],
            'events.*.resource_id' => ['required', Rule::in([$resourceId])],
            'events.*.payload' => ['required', 'array'],
            'events.*.payload.resource_id' => ['required', Rule::in([$resourceId])],
            'events.*.payload.operation_id' => ['required', 'uuid'],
            'events.*.payload.status' => ['required', Rule::in(['succeeded', 'needs_intervention', 'executing', 'failed', 'denied'])],
            'events.*.payload.generation' => ['required_if:events.*.payload.status,succeeded,executing,failed', 'integer', 'min:0'],
        ])->validate();
        $context->getConnection()->transaction(function () use ($actor, $context, $resourceId, $events): void {
            $context = RuntimeContext::query()->lockForUpdate()->findOrFail($context->id);
            Gate::forUser($actor)->authorize('manageRuntime', $context->server);
            foreach ($events as $event) {
                $payload = $event['payload'];
                $hash = hash('sha256', json_encode(Arr::sortRecursive($payload), JSON_THROW_ON_ERROR));
                $recorded = NodeEvent::query()->firstOrCreate(['runtime_context_id' => $context->id, 'sequence' => $event['sequence']], [
                    'resource_id' => $resourceId, 'operation_uuid' => $payload['operation_id'], 'payload' => $payload, 'payload_hash' => $hash,
                ]);
                if (! hash_equals($recorded->payload_hash, $hash) || $recorded->resource_id !== $resourceId) {
                    throw new RuntimeException('Node event sequence conflicts with its recorded outcome.');
                }
                $operation = NodeOperation::query()->where('runtime_context_id', $context->id)->where('resource_id', $resourceId)
                    ->where('uuid', $payload['operation_id'])->lockForUpdate()->first();
                if ($operation?->action === 'stage' && $payload['status'] === 'succeeded') {
                    $this->stagingValidator->validateOutcome($operation->request, $payload);
                }
                if ($operation?->action === 'activate' && in_array($payload['status'], ['executing', 'succeeded', 'failed'], true)) {
                    $this->activationValidator->validateOutcome($operation->request, $payload);
                } elseif ($operation && in_array($payload['status'], ['executing', 'failed'], true)) {
                    throw new RuntimeException('Unexpected activation outcome for a different operation.');
                }
                if ($operation && ($operation->last_node_sequence === null || $event['sequence'] > $operation->last_node_sequence)) {
                    $updates = ['last_node_sequence' => $event['sequence']];
                    if (! $operation->isTerminal()) {
                        $updates += ['status' => $payload['status'], 'result' => $payload, 'error_code' => $payload['code'] ?? null,
                            'attempt_uuid' => (string) Str::uuid(), 'lease_expires_at' => null];
                    }
                    $operation->update($updates);
                }
            }
        });
    }
}
