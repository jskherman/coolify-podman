<?php

namespace App\Jobs;

use App\Actions\Server\ReceiveNodeEvents;
use App\Models\NodeOperation;
use App\Models\RuntimeContext;
use App\Models\User;
use App\Services\NodeActivationValidator;
use App\Services\NodeExecutorTransport;
use App\Services\NodeStagingValidator;
use Illuminate\Contracts\Queue\ShouldQueue;
use Illuminate\Foundation\Queue\Queueable;
use Illuminate\Support\Facades\Gate;
use Illuminate\Support\Str;
use Throwable;

class ExecuteNodeOperationJob implements ShouldQueue
{
    use Queueable;

    public int $timeout = 80;

    public int $tries = 1;

    public function __construct(public int $operationId, public int $actorId) {}

    public function handle(NodeExecutorTransport $transport): void
    {
        $operation = (new NodeOperation)->getConnection()->transaction(function (): ?NodeOperation {
            $operation = NodeOperation::query()->lockForUpdate()->find($this->operationId);
            if (! $operation || $operation->isTerminal() || $operation->lease_expires_at?->isFuture()) {
                return null;
            }
            $actor = User::query()->find($this->actorId);
            $context = $operation->runtimeContext;
            $code = null;
            if (! $actor || Gate::forUser($actor)->denies('manageRuntime', $context->server)) {
                $code = 'authorization_revoked';
            } elseif (! hash_equals($context->connection_fingerprint, RuntimeContext::connectionFingerprint($context->server))
                || ! hash_equals($operation->transport_fingerprint, $context->transportFingerprint())) {
                $code = 'transport_changed';
            }
            if ($code) {
                $operation->update(['status' => $operation->attempts > 0 ? 'needs_intervention' : 'denied', 'error_code' => $code, 'lease_expires_at' => null]);

                return null;
            }
            $operation->update(['status' => 'executing', 'attempts' => $operation->attempts + 1,
                'attempt_uuid' => (string) Str::uuid(), 'lease_expires_at' => now()->addSeconds(90), 'error_code' => null]);

            return $operation;
        });
        if (! $operation) {
            return;
        }
        try {
            $result = $transport->execute($operation->runtimeContext, $operation->request);
            $updates = ['status' => $result['status'], 'result' => $result, 'error_code' => $result['code'] ?? null];
            if ($result['status'] === 'denied' && $operation->attempts > 1) {
                $updates['status'] = 'needs_intervention';
            }
        } catch (Throwable) {
            $updates = ['status' => 'uncertain', 'error_code' => 'transport_unavailable'];
        }
        try {
            $operation->getConnection()->transaction(function () use ($operation, $updates): void {
                $context = RuntimeContext::query()->lockForUpdate()->findOrFail($operation->runtime_context_id);
                $current = NodeOperation::query()->whereKey($operation->id)->lockForUpdate()->firstOrFail();
                if ($current->attempt_uuid !== $operation->attempt_uuid) {
                    return;
                }
                $current->update([...$updates, 'lease_expires_at' => null]);
                if ($current->action === 'events' && $current->status === 'succeeded') {
                    ReceiveNodeEvents::run(User::query()->findOrFail($this->actorId), $context, $current->resource_id, $updates['result']['events']);
                    $this->advanceEventCursor($context, $current);
                }
                $recovered = $updates['result']['recovered_operation'] ?? null;
                if ($current->action === 'recover' && $current->status === 'succeeded' && is_array($recovered)
                    && ($recovered['operation_id'] ?? null) === $current->target_operation_id
                    && ($recovered['resource_id'] ?? null) === $current->resource_id
                    && in_array($recovered['status'] ?? null, ['succeeded', 'failed'], true)) {
                    $target = NodeOperation::query()->where('runtime_context_id', $current->runtime_context_id)
                        ->where('resource_id', $current->resource_id)->where('uuid', $current->target_operation_id)->lockForUpdate()->first();
                    if ($target?->action === 'stage') {
                        app(NodeStagingValidator::class)->validateOutcome($target->request, $recovered);
                    }
                    if ($target?->action === 'activate') {
                        app(NodeActivationValidator::class)->validateOutcome($target->request, $recovered);
                    } elseif ($target && $recovered['status'] === 'failed') {
                        throw new \RuntimeException('Failed recovery outcome requires an activation target.');
                    }
                    if ($target && ! $target->isTerminal()) {
                        $target->update(['status' => $recovered['status'], 'result' => $recovered, 'error_code' => null,
                            'attempt_uuid' => (string) Str::uuid(), 'lease_expires_at' => null]);
                    }
                }
            });
        } catch (Throwable) {
            NodeOperation::query()->whereKey($operation->id)->where('attempt_uuid', $operation->attempt_uuid)
                ->update(['status' => 'needs_intervention', 'error_code' => 'outcome_publication_failed', 'lease_expires_at' => null]);
        }
    }

    private function advanceEventCursor(RuntimeContext $context, NodeOperation $current): void
    {
        $cursors = $context->event_cursors ?? [];
        $cursor = $cursors[$current->resource_id] ?? 0;
        $pages = NodeOperation::query()->where('runtime_context_id', $context->id)->where('resource_id', $current->resource_id)
            ->where('action', 'events')->where('status', 'succeeded')->latest('id')->limit(100)->get()->push($current);
        do {
            $previous = $cursor;
            foreach ($pages as $page) {
                if ($page->cursor <= $cursor) {
                    $cursor = max($cursor, $page->result['next_cursor']);
                }
            }
        } while ($cursor > $previous);
        $cursors[$current->resource_id] = $cursor;
        $context->update(['event_cursors' => $cursors]);
    }
}
