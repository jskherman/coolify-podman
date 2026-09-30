<?php

namespace App\Livewire\Server;

use App\Actions\Server\ProbeRuntimeCapabilities;
use App\Exceptions\RuntimeCapabilityProbeFailed;
use App\Models\RuntimeContext;
use App\Models\Server;
use Illuminate\Contracts\View\View;
use Illuminate\Foundation\Auth\Access\AuthorizesRequests;
use Livewire\Attributes\Locked;
use Livewire\Component;

class RuntimeCapabilities extends Component
{
    use AuthorizesRequests;

    #[Locked]
    public Server $server;

    public function mount(Server $server): void
    {
        $this->authorize('manageRuntime', $server);
        $this->server = $server;
    }

    public function probe(): void
    {
        $this->authorize('manageRuntime', $this->server);
        try {
            ProbeRuntimeCapabilities::run(auth()->user(), $this->server);
        } catch (RuntimeCapabilityProbeFailed $exception) {
            $this->dispatch('error', $exception->getMessage());

            return;
        }
        $this->dispatch('success', 'Runtime capabilities observed.');
    }

    public function render(): View
    {
        $this->authorize('manageRuntime', $this->server);

        return view('livewire.server.runtime-capabilities', [
            'contexts' => RuntimeContext::query()->where('server_id', $this->server->id)
                ->where('connection_fingerprint', RuntimeContext::connectionFingerprint($this->server))
                ->orderByDesc('probed_at')->get(),
        ]);
    }
}
