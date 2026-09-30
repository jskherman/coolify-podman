<?php

namespace App\Services;

use App\Models\RuntimeContext;
use Illuminate\Support\Facades\Process;
use RuntimeException;

class NodeExecutorTransport
{
    public const MAX_REQUEST_BYTES = 16384;

    public function __construct(private NodeStagingValidator $stagingValidator, private NodeActivationValidator $activationValidator) {}

    /** @param array<string, mixed> $request
     * @return array<string, mixed>
     */
    public function execute(RuntimeContext $context, array $request): array
    {
        $encodedRequest = json_encode($request, JSON_THROW_ON_ERROR)."\n";
        if (strlen($encodedRequest) > self::MAX_REQUEST_BYTES) {
            throw new RuntimeException('The encoded node request must not exceed 16384 bytes.');
        }
        if ($request['action'] === 'stage') {
            $this->stagingValidator->expectedBundle($request);
        }
        if ($request['action'] === 'activate') {
            $this->activationValidator->validateRequest($request);
        }
        $server = $context->server;
        $key = $context->executorPrivateKey;
        if (! $key || $key->team_id !== $server->team_id || $key->id === $server->private_key_id
            || $context->execution_uid < 1 || $context->manager !== 'user'
            || $server->settings->is_cloudflare_tunnel || $server->settings->is_jump_server
            || ! preg_match('/\A[a-z_][a-z0-9_-]*\z/i', $server->user)
            || ! preg_match('/\A[a-z0-9.:_-]+\z/i', $server->ip)
            || str_starts_with($server->ip, '-') || $server->port < 1 || $server->port > 65535
            || ! preg_match('/\Assh-ed25519 [A-Za-z0-9+\/=]+\z/', $context->ssh_host_key ?? '')) {
            throw new RuntimeException('Restricted executor transport is not configured for this identity.');
        }
        $files = [];
        try {
            foreach ([$key->private_key."\n", 'coolify-node '.$context->ssh_host_key."\n"] as $contents) {
                $path = tempnam(sys_get_temp_dir(), 'coolify-node-');
                if ($path === false) {
                    throw new RuntimeException('Cannot prepare restricted transport.');
                }
                $files[] = $path;
                if (! chmod($path, 0600) || file_put_contents($path, $contents) !== strlen($contents)) {
                    throw new RuntimeException('Cannot prepare restricted transport.');
                }
            }
            $command = ['ssh', '-F', '/dev/null', '-T', '-p', (string) $server->port, '-i', $files[0]];
            foreach (['BatchMode=yes', 'IdentitiesOnly=yes', 'IdentityAgent=none', 'ClearAllForwardings=yes',
                'PermitLocalCommand=no', 'StrictHostKeyChecking=yes', 'UpdateHostKeys=no', 'VerifyHostKeyDNS=no',
                'CheckHostIP=no', 'HostKeyAlias=coolify-node', 'HostKeyAlgorithms=ssh-ed25519',
                'GlobalKnownHostsFile=/dev/null', 'UserKnownHostsFile='.$files[1], 'ConnectTimeout=10',
                'ServerAliveInterval=10', 'ServerAliveCountMax=2', 'ControlMaster=no', 'ControlPath=none',
            ] as $option) {
                array_push($command, '-o', $option);
            }
            array_push($command, '-l', $server->user, '--', $server->ip, 'coolify-node-v1');
            $result = Process::input($encodedRequest)->timeout(65)->run($command);
            if (! $result->successful() || strlen($result->output()) > 32768) {
                throw new RuntimeException('Executor reply unavailable; reconcile the same operation.');
            }
            $outcome = json_decode($result->output(), true, 32, JSON_THROW_ON_ERROR);
            if (! is_array($outcome) || ! in_array($outcome['status'] ?? null, ['succeeded', 'denied', 'needs_intervention', 'executing', 'failed'], true)
                || (in_array($outcome['status'], ['executing', 'failed'], true) && $request['action'] !== 'activate')
                || in_array($outcome['code'] ?? '', ['executor_busy', 'executor_unavailable'], true)
                || ($outcome['status'] !== 'denied' && ($outcome['operation_id'] ?? null) !== $request['operation_id'])
                || ($outcome['status'] === 'succeeded' && (($outcome['resource_id'] ?? null) !== $request['resource_id'] || ! is_int($outcome['generation'] ?? null)))) {
                throw new RuntimeException('Executor reply is inconclusive; reconcile the same operation.');
            }

            if ($request['action'] === 'activate' && in_array($outcome['status'], ['executing', 'succeeded', 'failed'], true)) {
                $this->activationValidator->validateOutcome($request, $outcome);
            }

            if ($outcome['status'] === 'succeeded' && $request['action'] === 'stage') {
                $this->stagingValidator->validateOutcome($request, $outcome);
            }

            if ($outcome['status'] === 'succeeded' && $request['action'] === 'recover') {
                $recovered = $outcome['recovered_operation'] ?? null;
                if (! is_array($recovered) || ($recovered['operation_id'] ?? null) !== $request['target_operation_id']
                    || ($recovered['resource_id'] ?? null) !== $request['resource_id']
                    || ! in_array($recovered['status'] ?? null, ['succeeded', 'failed'], true) || ! is_int($recovered['generation'] ?? null)) {
                    throw new RuntimeException('Recovery reply does not match the authorized target.');
                }
            }

            if ($outcome['status'] === 'succeeded' && $request['action'] === 'events') {
                $events = $outcome['events'] ?? null;
                $cursor = $outcome['next_cursor'] ?? null;
                if (! is_array($events) || ! array_is_list($events) || count($events) > 25
                    || ! is_int($cursor) || $cursor < $request['cursor']) {
                    throw new RuntimeException('Invalid node event page.');
                }
                $previous = $request['cursor'];
                foreach ($events as $event) {
                    if (! is_array($event) || ! is_int($event['sequence'] ?? null) || $event['sequence'] <= $previous
                        || ($event['resource_id'] ?? null) !== $request['resource_id'] || ! is_array($event['payload'] ?? null)) {
                        throw new RuntimeException('Invalid node event scope or ordering.');
                    }
                    $previous = $event['sequence'];
                }
                if ($previous !== $cursor) {
                    throw new RuntimeException('Node event cursor does not match the delivered page.');
                }
            }

            return $outcome;
        } finally {
            foreach ($files as $path) {
                unlink($path);
            }
        }
    }
}
