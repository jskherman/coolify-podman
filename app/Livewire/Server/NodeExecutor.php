<?php

namespace App\Livewire\Server;

use App\Actions\Server\ConfigureNodeExecutor;
use App\Actions\Server\SubmitNodeOperation;
use App\Jobs\ExecuteNodeOperationJob;
use App\Models\NodeOperation;
use App\Models\PrivateKey;
use App\Models\RuntimeContext;
use Illuminate\Contracts\View\View;
use Illuminate\Foundation\Auth\Access\AuthorizesRequests;
use Illuminate\Support\Str;
use Livewire\Attributes\Locked;
use Livewire\Component;

class NodeExecutor extends Component
{
    use AuthorizesRequests;

    #[Locked]
    public RuntimeContext $context;

    public string $private_key_uuid = '';

    public string $ssh_host_key = '';

    public int $controller_epoch = 1;

    public int $policy_version = 1;

    public function mount(RuntimeContext $context): void
    {
        $this->authorize('manageRuntime', $context->server);
        $this->context = $context;
        $this->private_key_uuid = $context->executorPrivateKey?->uuid ?? '';
        $this->ssh_host_key = $context->ssh_host_key ?? '';
        $this->controller_epoch = $context->controller_epoch ?? 1;
        $this->policy_version = $context->policy_version ?? 1;
    }

    public function save(): void
    {
        $this->context = ConfigureNodeExecutor::run(auth()->user(), $this->context, [
            'private_key_uuid' => $this->private_key_uuid, 'ssh_host_key' => $this->ssh_host_key,
            'controller_epoch' => $this->controller_epoch, 'policy_version' => $this->policy_version,
        ]);
        $this->dispatch('success', 'Executor connection saved.');
    }

    public function reconcile(string $uuid): void
    {
        $this->authorize('manageRuntime', $this->context->server);
        $operation = NodeOperation::query()->where('runtime_context_id', $this->context->id)->where('uuid', $uuid)->firstOrFail();
        if (! $operation->isTerminal()) {
            ExecuteNodeOperationJob::dispatch($operation->id, auth()->id())->afterCommit();
        }
    }

    public function refreshEvents(string $resourceId): void
    {
        $this->authorize('manageRuntime', $this->context->server);
        abort_unless(NodeOperation::query()->where('runtime_context_id', $this->context->id)->where('resource_id', $resourceId)->exists(), 404);
        $cursor = $this->context->fresh()->event_cursors[$resourceId] ?? 0;
        SubmitNodeOperation::run(auth()->user(), $this->context, ['resource_id' => $resourceId, 'action' => 'events',
            'expected_generation' => 0, 'cursor' => $cursor, 'idempotency_key' => (string) Str::uuid()]);
    }

    public function render(): View
    {
        $this->authorize('manageRuntime', $this->context->server);

        return view('livewire.server.node-executor', [
            'keys' => PrivateKey::query()->where('team_id', $this->context->server->team_id)
                ->where('id', '!=', $this->context->server->private_key_id)->where('is_git_related', false)->get(['id', 'uuid', 'name']),
            'operations' => NodeOperation::query()->where('runtime_context_id', $this->context->id)->latest('id')->limit(20)->get(),
        ]);
    }
}
