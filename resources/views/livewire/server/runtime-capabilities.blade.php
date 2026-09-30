<x-application.settings-section title="Runtime capabilities"
    helper="Observe prerequisites as this server's SSH user. A successful probe does not enable Podman deployments.">
    <x-slot:actions>
        <x-forms.button wire:click="probe" wire:loading.attr="disabled" wire:target="probe">
            Probe capabilities
        </x-forms.button>
    </x-slot:actions>
    <div class="flex flex-col gap-4">
        @forelse ($contexts as $context)
            <div wire:key="runtime-context-{{ $context->uuid }}" class="flex flex-col gap-2">
                <p class="text-sm font-medium">{{ $context->runtime }}: UID {{ $context->execution_uid }}, GID {{ $context->execution_gid }} ({{ $context->manager }} manager)</p>
                <p class="text-xs text-neutral-500 dark:text-fg-dim">Observed {{ $context->probed_at->diffForHumans() }}</p>
                @foreach ($context->capabilities as $name => $capability)
                    <div class="text-sm">
                        <span class="font-medium">{{ $name }}: {{ $capability['available'] ? 'Available' : 'Unavailable' }}</span>
                        @if ($capability['remediation'])
                            <p class="text-xs text-neutral-500 dark:text-fg-dim">{{ $capability['remediation'] }}</p>
                        @endif
                    </div>
                @endforeach
                @if ($context->execution_uid > 0 && $context->manager === 'user')
                    <livewire:server.node-executor :context="$context" :key="'node-executor-'.$context->uuid" />
                @endif
            </div>
        @empty
            <p class="text-sm text-neutral-500 dark:text-fg-dim">No observations for the current SSH identity.</p>
        @endforelse
    </div>
</x-application.settings-section>
