<?php

use App\Data\ApplicationSpec;
use App\Models\PrivateKey;
use App\Models\RuntimeContext;
use App\Models\Server;
use App\Models\ServerSetting;
use App\Services\NodeExecutorTransport;
use App\Services\QuadletCompiler;
use Illuminate\Support\Facades\Process;

beforeEach(function () {
    $key = new PrivateKey;
    $key->forceFill(['id' => 2, 'team_id' => 3, 'private_key' => "fixture-private-key\n"]);
    $server = new Server;
    $server->forceFill(['id' => 9, 'team_id' => 3, 'private_key_id' => 1, 'ip' => '127.0.0.1', 'port' => 2222, 'user' => 'workload']);
    $server->setRelation('settings', new ServerSetting);
    $this->context = new RuntimeContext(['execution_uid' => 1001, 'manager' => 'user', 'ssh_host_key' => 'ssh-ed25519 AAAA']);
    $this->context->setRelation('server', $server)->setRelation('executorPrivateKey', $key);
    $this->request = ['action' => 'status', 'operation_id' => 'operation', 'resource_id' => 'resource'];
    Process::preventStrayProcesses();
});

test('transport uses isolated SSH arguments, pinned host key and protocol stdin', function () {
    $temporaryPaths = [];
    Process::fake(function ($process) use (&$temporaryPaths) {
        $command = $process->command;
        expect($command)->toBeArray()->toContain('StrictHostKeyChecking=yes', 'GlobalKnownHostsFile=/dev/null', 'ClearAllForwardings=yes', 'IdentityAgent=none', 'ControlPath=none');
        expect(array_slice($command, -5))->toBe(['-l', 'workload', '--', '127.0.0.1', 'coolify-node-v1']);
        expect(json_decode($process->input, true))->toBe($this->request);
        $temporaryPaths[] = $command[array_search('-i', $command) + 1];
        $hostFile = substr(collect($command)->first(fn ($arg) => str_starts_with($arg, 'UserKnownHostsFile=')), strlen('UserKnownHostsFile='));
        $temporaryPaths[] = $hostFile;
        expect(file_get_contents($hostFile))->toBe("coolify-node ssh-ed25519 AAAA\n");
        foreach ($temporaryPaths as $path) {
            expect(fileperms($path) & 0777)->toBe(0600);
        }

        return Process::result(output: json_encode(['status' => 'succeeded', ...$this->request, 'generation' => 4]));
    });
    expect(app(NodeExecutorTransport::class)->execute($this->context, $this->request)['generation'])->toBe(4);
    foreach ($temporaryPaths as $path) {
        expect(file_exists($path))->toBeFalse();
    }
});

test('transport rejects inconclusive or mismatched replies', function (string $reply, int $exitCode) {
    Process::fake(['*' => Process::result(output: $reply, exitCode: $exitCode)]);
    app(NodeExecutorTransport::class)->execute($this->context, $this->request);
})->with([
    ['lost', 255], ['not-json', 0],
    ['{"status":"denied","code":"executor_busy"}', 0],
    ['{"status":"denied","code":"executor_unavailable"}', 0],
    ['{"status":"succeeded","operation_id":"wrong","resource_id":"resource","generation":4}', 0],
])->throws(Exception::class);

test('transport cannot reuse broad credentials or escape the target argument', function (string $field, mixed $value) {
    $this->context->server->setAttribute($field, $value);
    try {
        app(NodeExecutorTransport::class)->execute($this->context, $this->request);
        test()->fail('Unsafe transport was accepted');
    } catch (RuntimeException) {
        Process::assertNothingRan();
    }
})->with([['private_key_id', 2], ['team_id', 99], ['ip', '-oProxyCommand=id'], ['user', 'root;id']]);

test('recovery reply cannot substitute another operation or resource', function () {
    $request = [...$this->request, 'action' => 'recover', 'target_operation_id' => 'target'];
    Process::fake(['*' => Process::result(output: json_encode([
        'status' => 'succeeded', 'operation_id' => 'operation', 'resource_id' => 'resource', 'generation' => 2,
        'recovered_operation' => ['status' => 'succeeded', 'operation_id' => 'foreign', 'resource_id' => 'resource', 'generation' => 2],
    ]))]);
    app(NodeExecutorTransport::class)->execute($this->context, $request);
})->throws(RuntimeException::class);

test('event cursor cannot skip undelivered observations', function () {
    $request = [...$this->request, 'action' => 'events', 'cursor' => 0];
    Process::fake(['*' => Process::result(output: json_encode([
        'status' => 'succeeded', 'operation_id' => 'operation', 'resource_id' => 'resource', 'generation' => 2,
        'events' => [], 'next_cursor' => 999,
    ]))]);
    app(NodeExecutorTransport::class)->execute($this->context, $request);
})->throws(RuntimeException::class);

function stagingTransportRequest(): array
{
    return ['protocol' => 2, 'action' => 'stage', 'operation_id' => 'operation',
        'resource_id' => '11111111-1111-4111-8111-111111111111', 'expected_generation' => 1,
        'spec' => json_decode(file_get_contents(base_path('tests/Fixtures/quadlet/prebuilt-v1/spec.json')), true)];
}

function stagingTransportReply(array $request): array
{
    $spec = ApplicationSpec::fromArray($request['spec']);
    $files = app(QuadletCompiler::class)->compile($spec, $request['resource_id']);

    return ['status' => 'succeeded', 'operation_id' => $request['operation_id'], 'resource_id' => $request['resource_id'],
        'generation' => $request['expected_generation'], 'bundle' => ['state' => 'staged', 'resource_id' => $request['resource_id'],
            'compiler_version' => QuadletCompiler::VERSION, 'spec_hash' => $spec->hash(),
            'bundle_hash' => hash('sha256', json_encode($files, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_LINE_TERMINATORS))]];
}

test('transport accepts staging only when the compiled bundle and generation match', function () {
    $request = stagingTransportRequest();
    $reply = stagingTransportReply($request);
    Process::fake(['*' => Process::result(output: json_encode($reply))]);
    expect(app(NodeExecutorTransport::class)->execute($this->context, $request))->toBe($reply);
});

test('transport accepts bounded generator validation evidence alongside verified staging hashes', function () {
    $request = stagingTransportRequest();
    $reply = stagingTransportReply($request);
    $reply['bundle']['validation'] = ['podman_version' => '5.4.2', 'generator_sha256' => str_repeat('a', 64)];
    Process::fake(['*' => Process::result(output: json_encode($reply))]);
    expect(app(NodeExecutorTransport::class)->execute($this->context, $request))->toBe($reply);
});

test('transport rejects forged staging acknowledgments', function (string $field, mixed $value) {
    $request = stagingTransportRequest();
    $reply = stagingTransportReply($request);
    data_set($reply, $field, $value);
    Process::fake(['*' => Process::result(output: json_encode($reply))]);
    app(NodeExecutorTransport::class)->execute($this->context, $request);
})->with([
    ['bundle', null], ['bundle.state', 'active'], ['bundle.resource_id', 'foreign'], ['bundle.compiler_version', 'untrusted'],
    ['bundle.spec_hash', str_repeat('0', 64)], ['bundle.bundle_hash', str_repeat('f', 64)],
    ['bundle.bundle_hash', strtoupper(str_repeat('a', 64))], ['bundle.bundle_hash', []], ['generation', 2],
    ['bundle.validation', ['podman_version' => '5.4.2', 'generator_sha256' => 'not-a-hash']],
    ['bundle.validation', ['podman_version' => "5.4.2\ncommand", 'generator_sha256' => str_repeat('a', 64)]],
])->throws(RuntimeException::class);

test('transport rejects an oversized staging request before starting SSH', function () {
    $request = stagingTransportRequest();
    $request['spec']['command'] = array_fill(0, 4, str_repeat('é', 1024));
    try {
        app(NodeExecutorTransport::class)->execute($this->context, $request);
        test()->fail('Oversized request reached transport');
    } catch (RuntimeException $exception) {
        expect($exception->getMessage())->toContain('16384');
        Process::assertNothingRan();
    }
});

test('transport rejects legacy or noncanonical staging requests before SSH', function (string $invalid) {
    $request = stagingTransportRequest();
    if ($invalid === 'legacy') {
        $request['protocol'] = 1;
    } else {
        unset($request['spec']['profile']);
    }
    try {
        app(NodeExecutorTransport::class)->execute($this->context, $request);
        test()->fail('Invalid staging protocol reached transport');
    } catch (RuntimeException) {
        Process::assertNothingRan();
    }
})->with(['legacy', 'noncanonical']);

test('transport accepts scoped executing and compensated activation replies', function (string $status) {
    $request = ['protocol' => 2, 'action' => 'activate', 'operation_id' => 'operation', 'resource_id' => 'resource',
        'expected_generation' => 2, 'bundle_hash' => str_repeat('a', 64)];
    $reply = ['status' => $status, 'operation_id' => 'operation', 'resource_id' => 'resource',
        'generation' => $status === 'executing' ? 2 : 3,
        'release' => ['state' => $status === 'executing' ? 'activating' : 'rolled_back',
            'bundle_hash' => str_repeat('a', 64), 'previous_bundle_hash' => str_repeat('b', 64)]];
    if ($status === 'failed') {
        $reply['release']['data_recovery'] = false;
        $reply['observed'] = ['active' => true, 'health' => true, 'bundle_hash' => str_repeat('b', 64)];
    }
    Process::fake(['*' => Process::result(output: json_encode($reply))]);
    expect(app(NodeExecutorTransport::class)->execute($this->context, $request))->toBe($reply);
})->with(['executing', 'failed']);

test('transport cannot turn a generic lifecycle request into an activation reply', function () {
    Process::fake(['*' => Process::result(output: json_encode(['status' => 'executing', 'operation_id' => 'operation', 'resource_id' => 'resource']))]);
    app(NodeExecutorTransport::class)->execute($this->context, $this->request);
})->throws(RuntimeException::class);
