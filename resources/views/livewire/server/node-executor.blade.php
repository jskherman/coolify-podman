<div class="flex flex-col gap-4">
    <details class="text-sm">
        <summary class="cursor-pointer font-medium">Restricted node executor</summary>
        <form wire:submit="save" class="mt-4 flex flex-col gap-4">
            <p class="text-xs text-neutral-500 dark:text-fg-dim">Connect an installed executor using its dedicated restricted key and verified host public key. Authority versions must match the node policy. Changing them does not transfer ownership of pending operations.</p>
            <x-forms.select id="private_key_uuid" label="Restricted credential" required>
                <option value="">Select a dedicated key</option>
                @foreach ($keys as $key)
                    <option value="{{ $key->uuid }}">{{ $key->name }}</option>
                @endforeach
            </x-forms.select>
            <x-forms.input id="ssh_host_key" label="Verified Ed25519 host public key" required />
            <div class="grid grid-cols-1 gap-4 md:grid-cols-2">
                <x-forms.input id="controller_epoch" type="number" min="1" label="Controller epoch" required />
                <x-forms.input id="policy_version" type="number" min="1" label="Policy version" required />
            </div>
            <div><x-forms.button type="submit" wire:loading.attr="disabled" wire:target="save">Save executor connection</x-forms.button></div>
        </form>
    </details>
    <div wire:poll.10s class="flex flex-col gap-2">
        <h4 class="text-sm font-medium">Node operations</h4>
        @forelse ($operations as $operation)
            <div wire:key="node-operation-{{ $operation->uuid }}" class="flex flex-wrap items-center justify-between gap-2 border-b border-neutral-200 py-2 dark:border-white/[0.08]">
                <div class="min-w-0 text-xs">
                    <p>{{ $operation->action }}: {{ $operation->status }}{{ $operation->error_code ? ' ('.$operation->error_code.')' : '' }}</p>
                    <p class="break-all text-neutral-500 dark:text-fg-dim">{{ $operation->uuid }}</p>
                    @if (isset($operation->result['generation']))
                        <p>Observed generation {{ $operation->result['generation'] }}</p>
                    @endif
                    @if (is_bool(data_get($operation->result, 'observed.boot_enabled')))
                        <p>Observed boot activation: {{ $operation->result['observed']['boot_enabled'] ? 'enabled' : 'disabled' }}</p>
                    @endif
                </div>
                <div class="flex items-center gap-2">
                    <x-forms.button wire:click="refreshEvents('{{ $operation->resource_id }}')" wire:loading.attr="disabled">Read node events</x-forms.button>
                @if (! $operation->isTerminal())
                    <x-forms.button wire:click="reconcile('{{ $operation->uuid }}')" wire:loading.attr="disabled">Reconcile</x-forms.button>
                @endif
                </div>
            </div>
        @empty
            <p class="text-xs text-neutral-500 dark:text-fg-dim">No operations recorded for this runtime identity.</p>
        @endforelse
    </div>
</div>
