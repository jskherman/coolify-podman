<?php

namespace App\Actions\Server;

use App\Models\PrivateKey;
use App\Models\RuntimeContext;
use App\Models\User;
use Illuminate\Support\Facades\Gate;
use Illuminate\Support\Facades\Validator;
use Illuminate\Validation\ValidationException;
use Lorisleiva\Actions\Concerns\AsAction;
use phpseclib3\Crypt\PublicKeyLoader;

class ConfigureNodeExecutor
{
    use AsAction;

    /** @param array<string, mixed> $input */
    public function handle(User $actor, RuntimeContext $context, array $input): RuntimeContext
    {
        Gate::forUser($actor)->authorize('manageRuntime', $context->server);
        $data = Validator::make($input, [
            'private_key_uuid' => ['required', 'string'],
            'ssh_host_key' => ['required', 'string', 'max:256', 'regex:/\Assh-ed25519 [A-Za-z0-9+\/=]+\z/'],
            'controller_epoch' => ['required', 'integer', 'min:1'],
            'policy_version' => ['required', 'integer', 'min:1'],
        ])->validate();
        try {
            PublicKeyLoader::load($data['ssh_host_key']);
        } catch (\Throwable) {
            throw ValidationException::withMessages(['ssh_host_key' => 'Enter the verified Ed25519 host public key.']);
        }

        return $context->getConnection()->transaction(function () use ($context, $actor, $data): RuntimeContext {
            $context = RuntimeContext::query()->lockForUpdate()->findOrFail($context->id);
            Gate::forUser($actor)->authorize('manageRuntime', $context->server);
            abort_unless($context->execution_uid > 0 && $context->manager === 'user', 422, 'A dedicated rootless identity is required.');
            abort_unless(hash_equals($context->connection_fingerprint, RuntimeContext::connectionFingerprint($context->server)), 409, 'Probe the current SSH identity first.');
            $key = PrivateKey::query()->where('team_id', $context->server->team_id)->where('uuid', $data['private_key_uuid'])->firstOrFail();
            abort_if($key->id === $context->server->private_key_id || $key->is_git_related, 422, 'Use a separate restricted executor credential.');
            $context->update(['executor_private_key_id' => $key->id, 'ssh_host_key' => $data['ssh_host_key'],
                'controller_epoch' => $data['controller_epoch'], 'policy_version' => $data['policy_version']]);

            return $context;
        });
    }
}
