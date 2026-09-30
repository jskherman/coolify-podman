<?php

namespace App\Actions\Server;

use App\Data\ApplicationSpec;
use App\Jobs\ExecuteNodeOperationJob;
use App\Models\NodeOperation;
use App\Models\RuntimeContext;
use App\Models\User;
use App\Services\NodeExecutorTransport;
use Illuminate\Support\Arr;
use Illuminate\Support\Facades\Gate;
use Illuminate\Support\Facades\Validator;
use Illuminate\Support\Str;
use Illuminate\Validation\ValidationException;
use Lorisleiva\Actions\Concerns\AsAction;

class SubmitNodeOperation
{
    use AsAction;

    /** @param array<string, mixed> $input */
    public function handle(User $actor, RuntimeContext $context, array $input): NodeOperation
    {
        Gate::forUser($actor)->authorize('manageRuntime', $context->server);
        if (array_diff(array_keys($input), ['resource_id', 'action', 'expected_generation', 'idempotency_key', 'target_operation_id', 'cursor', 'spec', 'bundle_hash'])) {
            throw ValidationException::withMessages(['operation' => 'Unsupported operation fields. Raw commands, units and paths are not accepted.']);
        }
        if (array_key_exists('spec', $input) && ($input['action'] ?? null) !== 'stage') {
            throw ValidationException::withMessages(['spec' => 'Only stage operations accept an application specification.']);
        }
        $data = Validator::make($input, [
            'resource_id' => 'required|uuid', 'idempotency_key' => 'required|uuid',
            'action' => 'required|in:status,start,stop,restart,recover,events,stage,activate', 'expected_generation' => 'required|integer|min:0',
            'target_operation_id' => 'required_if:action,recover|prohibited_unless:action,recover|uuid',
            'cursor' => 'required_if:action,events|prohibited_unless:action,events|integer|min:0',
            'spec' => 'required_if:action,stage|array',
            'bundle_hash' => ['required_if:action,activate', 'prohibited_unless:action,activate', 'string', 'regex:/\A[a-f0-9]{64}\z/'],
        ])->validate();
        if ($data['action'] === 'stage') {
            $data['spec'] = ApplicationSpec::fromArray($data['spec'])->toArray();
        }
        $data['resource_id'] = strtolower($data['resource_id']);
        $data['idempotency_key'] = strtolower($data['idempotency_key']);
        $data['expected_generation'] = (int) $data['expected_generation'];
        if (isset($data['cursor'])) {
            $data['cursor'] = (int) $data['cursor'];
        }
        if (isset($data['target_operation_id'])) {
            $data['target_operation_id'] = strtolower($data['target_operation_id']);
        }
        $operation = $context->getConnection()->transaction(function () use ($context, $actor, $data): NodeOperation {
            $lockedContext = RuntimeContext::query()->whereKey($context->id)->lockForUpdate()->firstOrFail();
            Gate::forUser($actor)->authorize('manageRuntime', $lockedContext->server);
            $existing = NodeOperation::query()->where('runtime_context_id', $context->id)->where('idempotency_key', $data['idempotency_key'])->first();
            if ($existing) {
                abort_unless($existing->resource_id === $data['resource_id'] && $existing->action === $data['action'] && $existing->expected_generation === $data['expected_generation'] && $existing->target_operation_id === ($data['target_operation_id'] ?? null) && $existing->cursor === ($data['cursor'] ?? null) && ($existing->request['spec'] ?? null) === ($data['spec'] ?? null) && ($existing->request['bundle_hash'] ?? null) === ($data['bundle_hash'] ?? null), 409, 'Idempotency key is already bound to another intent.');

                return $existing;
            }
            abort_unless($lockedContext->executor_private_key_id && $lockedContext->ssh_host_key && $lockedContext->controller_epoch !== null && $lockedContext->policy_version !== null, 422, 'Configure the dedicated node credential, pinned host key and authority versions first.');
            abort_unless($lockedContext->execution_uid > 0 && $lockedContext->manager === 'user', 422, 'The initial executor profile requires a dedicated rootless identity.');
            abort_unless(hash_equals($lockedContext->connection_fingerprint, RuntimeContext::connectionFingerprint($lockedContext->server)), 409, 'Probe the changed server identity before submitting an operation.');
            $uuid = (string) Str::uuid();
            $request = ['protocol' => 2, 'execution_uid' => $lockedContext->execution_uid, 'execution_gid' => $lockedContext->execution_gid, 'operation_id' => $uuid, ...$data,
                'controller_epoch' => $lockedContext->controller_epoch, 'policy_version' => $lockedContext->policy_version,
                'deadline' => now()->addMinutes(2)->timestamp];
            if (strlen(json_encode($request, JSON_THROW_ON_ERROR)."\n") > NodeExecutorTransport::MAX_REQUEST_BYTES) {
                throw ValidationException::withMessages(['operation' => 'The encoded node request must not exceed 16384 bytes.']);
            }

            return NodeOperation::query()->create([
                'uuid' => $uuid, 'runtime_context_id' => $context->id, 'actor_id' => $actor->id,
                ...Arr::except($data, ['spec', 'bundle_hash']), 'transport_fingerprint' => $lockedContext->transportFingerprint(),
                'request' => $request,
            ]);
        });
        if (! $operation->isTerminal()) {
            ExecuteNodeOperationJob::dispatch($operation->id, $actor->id)->afterCommit();
        }

        return $operation;
    }
}
