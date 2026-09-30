<?php

use App\Actions\Server\ReceiveNodeEvents;
use App\Actions\Server\SubmitNodeOperation;
use App\Data\ApplicationSpec;
use App\Jobs\ExecuteNodeOperationJob;
use App\Livewire\Server\NodeExecutor;
use App\Models\InstanceSettings;
use App\Models\NodeEvent;
use App\Models\NodeOperation;
use App\Models\PrivateKey;
use App\Models\RuntimeContext;
use App\Models\Server;
use App\Models\Team;
use App\Models\User;
use App\Services\NodeExecutorTransport;
use App\Services\QuadletCompiler;
use Illuminate\Auth\Access\AuthorizationException;
use Illuminate\Database\Eloquent\ModelNotFoundException;
use Illuminate\Foundation\Testing\RefreshDatabase;
use Illuminate\Support\Facades\Queue;
use Illuminate\Support\Str;
use Illuminate\Validation\ValidationException;
use Livewire\Livewire;
use phpseclib3\Crypt\EC;
use Symfony\Component\HttpKernel\Exception\HttpException;

uses(RefreshDatabase::class);

beforeEach(function () {
    Server::flushIdentityMap();
    config(['app.maintenance.store' => 'array', 'cache.default' => 'array', 'cache.stores.redis.driver' => 'array']);
    InstanceSettings::forceCreate(['id' => 0, 'is_api_enabled' => true]);
    $this->team = Team::factory()->create();
    $this->actor = User::factory()->create();
    $this->team->members()->attach($this->actor, ['role' => 'owner']);
    session(['currentTeam' => $this->team]);
    $broadKey = PrivateKey::factory()->create(['team_id' => $this->team->id]);
    $this->server = Server::factory()->create(['private_key_id' => $broadKey->id, 'team_id' => $this->team->id, 'user' => 'workload', 'port' => 22]);
    $this->server->refresh();
    $key = PrivateKey::factory()->create(['team_id' => $this->team->id, 'private_key' => EC::createKey('Ed25519')->toString('OpenSSH')]);
    $this->context = RuntimeContext::factory()->create([
        'server_id' => $this->server->id,
        'connection_fingerprint' => RuntimeContext::connectionFingerprint($this->server),
        'executor_private_key_id' => $key->id, 'ssh_host_key' => trim($key->public_key),
        'controller_epoch' => 1, 'policy_version' => 1,
    ]);
    $this->input = ['resource_id' => (string) Str::uuid(), 'action' => 'stop', 'expected_generation' => 1, 'idempotency_key' => (string) Str::uuid()];
    Queue::fake();
    $this->transport = Mockery::mock(NodeExecutorTransport::class);
    app()->instance(NodeExecutorTransport::class, $this->transport);
});

test('submission persists immutable intent before dispatch and deduplicates it', function () {
    $first = SubmitNodeOperation::run($this->actor, $this->context, $this->input);
    $again = SubmitNodeOperation::run($this->actor, $this->context, $this->input);
    expect($first->uuid)->toBe($again->uuid)->and(NodeOperation::count())->toBe(1)
        ->and($first->request['operation_id'])->toBe($first->uuid)
        ->and($again->request)->toBe($first->request)->and($first->status)->toBe('queued');
    Queue::assertPushed(ExecuteNodeOperationJob::class);
    $this->transport->shouldNotReceive('execute');
});

test('a repeated idempotency key cannot change target or action', function () {
    SubmitNodeOperation::run($this->actor, $this->context, $this->input);
    SubmitNodeOperation::run($this->actor, $this->context, [...$this->input, 'action' => 'start']);
})->throws(HttpException::class);

test('worker journals a remote outcome and never reexecutes a completed operation', function () {
    $operation = SubmitNodeOperation::run($this->actor, $this->context, $this->input);
    $this->transport->shouldReceive('execute')->once()->andReturn([
        'status' => 'succeeded', 'operation_id' => $operation->uuid, 'resource_id' => $this->input['resource_id'],
        'generation' => 2, 'observed' => ['active' => false, 'invocation' => ''],
    ]);
    $job = new ExecuteNodeOperationJob($operation->id, $this->actor->id);
    $job->handle($this->transport);
    $job->handle($this->transport);
    expect($operation->fresh()->status)->toBe('succeeded')->and($operation->fresh()->result['generation'])->toBe(2)
        ->and($operation->fresh()->attempts)->toBe(1);
});

test('revoked administration prevents a queued operation from reaching the node', function () {
    $operation = SubmitNodeOperation::run($this->actor, $this->context, $this->input);
    $this->team->members()->updateExistingPivot($this->actor->id, ['role' => 'member']);
    $this->transport->shouldNotReceive('execute');
    (new ExecuteNodeOperationJob($operation->id, $this->actor->id))->handle($this->transport);
    expect($operation->fresh()->status)->toBe('denied')->and($operation->fresh()->error_code)->toBe('authorization_revoked');
});

test('transport loss retains the original request for reconciliation', function () {
    $operation = SubmitNodeOperation::run($this->actor, $this->context, $this->input);
    $original = $operation->request;
    $this->transport->shouldReceive('execute')->once()->andThrow(new RuntimeException('Lost reply'));
    (new ExecuteNodeOperationJob($operation->id, $this->actor->id))->handle($this->transport);
    expect($operation->fresh()->status)->toBe('uncertain')->and($operation->fresh()->request)->toBe($original);
    $this->transport->shouldReceive('execute')->once()->withArgs(fn ($context, $request) => $request === $original)
        ->andReturn(['status' => 'succeeded', 'operation_id' => $operation->uuid, 'resource_id' => $this->input['resource_id'], 'generation' => 2]);
    (new ExecuteNodeOperationJob($operation->id, $this->actor->id))->handle($this->transport);
    expect($operation->fresh()->status)->toBe('succeeded')->and($operation->fresh()->attempts)->toBe(2);
});

test('API rejects raw commands, members and foreign contexts', function () {
    $token = $this->actor->createToken('node', ['read', 'write'])->plainTextToken;
    $url = "/api/v1/servers/{$this->server->uuid}/runtime-contexts/{$this->context->uuid}/operations";
    $this->withToken($token)->postJson($url, [...$this->input, 'command' => 'id'])->assertUnprocessable();
    $foreign = RuntimeContext::factory()->create();
    $this->withToken($token)->postJson(str_replace($this->context->uuid, $foreign->uuid, $url), $this->input)->assertNotFound();
    $this->team->members()->updateExistingPivot($this->actor->id, ['role' => 'member']);
    $this->withToken($token)->getJson($url)->assertForbidden();
    $this->withToken($token)->postJson($url, $this->input)->assertForbidden();
    expect(NodeOperation::count())->toBe(0);
});

test('API accepts a scoped operation and exposes its persisted state', function () {
    $token = $this->actor->createToken('node', ['read', 'write'])->plainTextToken;
    $url = "/api/v1/servers/{$this->server->uuid}/runtime-contexts/{$this->context->uuid}/operations";
    $this->withToken($token)->postJson($url, $this->input)->assertAccepted()->assertJsonPath('status', 'queued');
    $this->withToken($token)->getJson($url)->assertOk()->assertJsonPath('0.status', 'queued');
});

test('an active lease prevents competing workers from issuing the same request', function () {
    $operation = SubmitNodeOperation::run($this->actor, $this->context, $this->input);
    $operation->update(['status' => 'executing', 'lease_expires_at' => now()->addMinute()]);
    $this->transport->shouldNotReceive('execute');
    (new ExecuteNodeOperationJob($operation->id, $this->actor->id))->handle($this->transport);
    expect($operation->fresh()->attempts)->toBe(0);
});

test('a changed transport blocks delivery and preserves uncertain effects', function (int $attempts, string $status) {
    $operation = SubmitNodeOperation::run($this->actor, $this->context, $this->input);
    $operation->update(['attempts' => $attempts]);
    $this->context->update(['policy_version' => 2]);
    $this->transport->shouldNotReceive('execute');
    (new ExecuteNodeOperationJob($operation->id, $this->actor->id))->handle($this->transport);
    expect($operation->fresh()->status)->toBe($status)->and($operation->fresh()->error_code)->toBe('transport_changed');
})->with([[0, 'denied'], [1, 'needs_intervention']]);

test('a denied reconciliation does not erase a potentially successful prior effect', function () {
    $operation = SubmitNodeOperation::run($this->actor, $this->context, $this->input);
    $operation->update(['status' => 'uncertain', 'attempts' => 1]);
    $this->transport->shouldReceive('execute')->once()->andReturn(['status' => 'denied', 'code' => 'stale_policy']);
    (new ExecuteNodeOperationJob($operation->id, $this->actor->id))->handle($this->transport);
    expect($operation->fresh()->status)->toBe('needs_intervention');
});

test('executor configuration requires a separate owned credential and pins authority without exposing secrets', function () {
    $token = $this->actor->createToken('node', ['read', 'write'])->plainTextToken;
    $url = "/api/v1/servers/{$this->server->uuid}/runtime-contexts/{$this->context->uuid}/executor";
    $input = ['private_key_uuid' => $this->context->executorPrivateKey->uuid,
        'ssh_host_key' => $this->context->ssh_host_key, 'controller_epoch' => 1, 'policy_version' => 1];
    $this->withToken($token)->patchJson($url, $input)->assertOk()->assertJsonMissingPath('ssh_host_key')->assertJsonMissingPath('executor_private_key');
    $foreign = PrivateKey::factory()->create(['team_id' => Team::factory(), 'private_key' => EC::createKey('Ed25519')->toString('OpenSSH')]);
    $this->withToken($token)->patchJson($url, [...$input, 'private_key_uuid' => $foreign->uuid])->assertNotFound();
    $this->withToken($token)->patchJson($url, [...$input, 'private_key_uuid' => $this->server->privateKey->uuid])->assertUnprocessable();
    $this->team->members()->updateExistingPivot($this->actor->id, ['role' => 'member']);
    $this->withToken($token)->patchJson($url, $input)->assertForbidden();
});

test('executor UI saves configuration and reconciles only its own operations', function () {
    $operation = SubmitNodeOperation::run($this->actor, $this->context, $this->input);
    $this->actingAs($this->actor);
    $component = Livewire::test(NodeExecutor::class, ['context' => $this->context])
        ->assertSee($operation->uuid)->call('save')->assertHasNoErrors()->assertDispatched('success')
        ->call('reconcile', $operation->uuid)->assertOk()
        ->call('refreshEvents', $operation->resource_id)->assertOk();
    Queue::assertPushed(ExecuteNodeOperationJob::class, 3);
    $foreignContext = RuntimeContext::factory()->create();
    $foreignOperation = $operation->replicate(['uuid', 'idempotency_key']);
    $foreignOperation->forceFill(['uuid' => (string) Str::uuid(), 'idempotency_key' => (string) Str::uuid(), 'runtime_context_id' => $foreignContext->id])->save();
    expect(fn () => $component->call('reconcile', $foreignOperation->uuid))->toThrow(ModelNotFoundException::class);
    $this->team->members()->updateExistingPivot($this->actor->id, ['role' => 'member']);
    Livewire::test(NodeExecutor::class, ['context' => $this->context])->assertForbidden();
});

test('late worker cannot overwrite the result of a replacement lease', function () {
    $operation = SubmitNodeOperation::run($this->actor, $this->context, $this->input);
    $this->transport->shouldReceive('execute')->once()->andReturnUsing(function () use ($operation) {
        $operation->update(['attempt_uuid' => (string) Str::uuid(), 'status' => 'succeeded', 'result' => ['generation' => 3]]);

        return ['status' => 'needs_intervention', 'code' => 'effect_unresolved'];
    });
    (new ExecuteNodeOperationJob($operation->id, $this->actor->id))->handle($this->transport);
    expect($operation->fresh()->status)->toBe('succeeded')->and($operation->fresh()->result['generation'])->toBe(3);
});

test('recovery sweep queues lost dispatches and expired leases without new operation IDs', function () {
    $operation = SubmitNodeOperation::run($this->actor, $this->context, $this->input);
    Queue::fake();
    $this->artisan('node:reconcile')->assertSuccessful();
    Queue::assertPushed(ExecuteNodeOperationJob::class, fn ($job) => $job->operationId === $operation->id);
    Queue::fake();
    $operation->update(['status' => 'executing', 'attempts' => 1, 'lease_expires_at' => now()->addMinute()]);
    $this->artisan('node:reconcile')->assertSuccessful();
    Queue::assertNothingPushed();
    $operation->update(['lease_expires_at' => now()->subSecond()]);
    $this->artisan('node:reconcile')->assertSuccessful();
    Queue::assertPushed(ExecuteNodeOperationJob::class, 1);
    Queue::fake();
    $operation->update(['attempts' => 5]);
    $this->artisan('node:reconcile')->assertSuccessful();
    Queue::assertNothingPushed();
    expect(NodeOperation::count())->toBe(1);
});

test('authorized recovery records the new intent and adopts only its scoped target outcome', function () {
    $target = SubmitNodeOperation::run($this->actor, $this->context, $this->input);
    $target->update(['status' => 'uncertain', 'attempts' => 1]);
    $this->context->update(['controller_epoch' => 2, 'policy_version' => 2]);
    $operation = SubmitNodeOperation::run($this->actor, $this->context, [
        ...$this->input, 'idempotency_key' => (string) Str::uuid(), 'action' => 'recover', 'target_operation_id' => $target->uuid,
    ]);
    $result = ['status' => 'succeeded', 'operation_id' => $target->uuid, 'resource_id' => $target->resource_id, 'generation' => 2];
    $this->transport->shouldReceive('execute')->once()->andReturn([
        'status' => 'succeeded', 'operation_id' => $operation->uuid, 'resource_id' => $target->resource_id,
        'generation' => 2, 'recovered_operation' => $result,
    ]);
    (new ExecuteNodeOperationJob($operation->id, $this->actor->id))->handle($this->transport);
    expect($operation->fresh()->status)->toBe('succeeded')->and($target->fresh()->status)->toBe('succeeded')
        ->and($target->fresh()->result)->toBe($result)->and($target->fresh()->request['controller_epoch'])->toBe(1);
});

test('outbox ingestion tolerates duplicates and reordered delivery without regressing operation state', function () {
    $operation = SubmitNodeOperation::run($this->actor, $this->context, $this->input);
    $pending = ['sequence' => 1, 'resource_id' => $operation->resource_id,
        'payload' => ['operation_id' => $operation->uuid, 'resource_id' => $operation->resource_id, 'status' => 'needs_intervention', 'code' => 'effect_unresolved']];
    $success = ['sequence' => 2, 'resource_id' => $operation->resource_id,
        'payload' => ['operation_id' => $operation->uuid, 'resource_id' => $operation->resource_id, 'status' => 'succeeded', 'generation' => 2]];
    ReceiveNodeEvents::run($this->actor, $this->context, $operation->resource_id, [$success]);
    ReceiveNodeEvents::run($this->actor, $this->context, $operation->resource_id, [$pending, $success]);
    expect(NodeEvent::count())->toBe(2)->and($operation->fresh()->status)->toBe('succeeded')
        ->and($operation->fresh()->last_node_sequence)->toBe(2)->and($operation->fresh()->result['generation'])->toBe(2);
});

test('outbox ingestion rejects foreign resources and conflicting sequence reuse atomically', function () {
    $operation = SubmitNodeOperation::run($this->actor, $this->context, $this->input);
    $event = ['sequence' => 1, 'resource_id' => $operation->resource_id,
        'payload' => ['operation_id' => $operation->uuid, 'resource_id' => $operation->resource_id, 'status' => 'succeeded', 'generation' => 2]];
    ReceiveNodeEvents::run($this->actor, $this->context, $operation->resource_id, [$event]);
    expect(fn () => ReceiveNodeEvents::run($this->actor, $this->context, (string) Str::uuid(), [$event]))->toThrow(ValidationException::class);
    expect(fn () => ReceiveNodeEvents::run($this->actor, $this->context, $operation->resource_id, [
        [...$event, 'payload' => [...$event['payload'], 'generation' => 99]],
    ]))->toThrow(RuntimeException::class);
    expect(NodeEvent::count())->toBe(1)->and($operation->fresh()->result['generation'])->toBe(2);
    $this->team->members()->updateExistingPivot($this->actor->id, ['role' => 'member']);
    expect(fn () => ReceiveNodeEvents::run($this->actor, $this->context, $operation->resource_id, [$event]))->toThrow(AuthorizationException::class);
});

test('event page delivery persists observations and exposes only the authorized context history', function () {
    $target = SubmitNodeOperation::run($this->actor, $this->context, $this->input);
    $operation = SubmitNodeOperation::run($this->actor, $this->context, [...$this->input,
        'idempotency_key' => (string) Str::uuid(), 'action' => 'events', 'expected_generation' => 0, 'cursor' => 0]);
    $this->transport->shouldReceive('execute')->once()->andReturn([
        'status' => 'succeeded', 'operation_id' => $operation->uuid, 'resource_id' => $target->resource_id, 'generation' => 2,
        'next_cursor' => 1, 'events' => [['sequence' => 1, 'resource_id' => $target->resource_id,
            'payload' => ['operation_id' => $target->uuid, 'resource_id' => $target->resource_id, 'status' => 'succeeded', 'generation' => 2]]],
    ]);
    (new ExecuteNodeOperationJob($operation->id, $this->actor->id))->handle($this->transport);
    expect($operation->fresh()->status)->toBe('succeeded')->and($target->fresh()->status)->toBe('succeeded');
    $token = $this->actor->createToken('events', ['read'])->plainTextToken;
    $url = "/api/v1/servers/{$this->server->uuid}/runtime-contexts/{$this->context->uuid}/events";
    $this->withToken($token)->getJson($url)->assertOk()->assertJsonPath('0.operation_uuid', $target->uuid)->assertJsonCount(1);
    $foreign = RuntimeContext::factory()->create();
    $this->withToken($token)->getJson(str_replace($this->context->uuid, $foreign->uuid, $url))->assertNotFound();
    $this->team->members()->updateExistingPivot($this->actor->id, ['role' => 'member']);
    $this->withToken($token)->getJson($url)->assertForbidden();
});

test('outbox polling advances only across delivered page boundaries after reordering', function () {
    $target = SubmitNodeOperation::run($this->actor, $this->context, $this->input);
    foreach ([[10, 20], [0, 10]] as [$cursor, $next]) {
        $page = SubmitNodeOperation::run($this->actor, $this->context, [...$this->input,
            'idempotency_key' => (string) Str::uuid(), 'action' => 'events', 'expected_generation' => 0, 'cursor' => $cursor]);
        $this->transport->shouldReceive('execute')->once()->andReturn([
            'status' => 'succeeded', 'operation_id' => $page->uuid, 'resource_id' => $target->resource_id, 'generation' => 2,
            'next_cursor' => $next, 'events' => [['sequence' => $next, 'resource_id' => $target->resource_id,
                'payload' => ['operation_id' => $target->uuid, 'resource_id' => $target->resource_id, 'status' => 'succeeded', 'generation' => 2]]],
        ]);
        (new ExecuteNodeOperationJob($page->id, $this->actor->id))->handle($this->transport);
        if ($cursor === 10) {
            $this->actingAs($this->actor);
            Livewire::test(NodeExecutor::class, ['context' => $this->context])->call('refreshEvents', $target->resource_id)->assertOk();
            expect(NodeOperation::query()->latest('id')->first()->cursor)->toBe(0);
        }
    }
    expect($this->context->fresh()->event_cursors[$target->resource_id])->toBe(20);
});

test('new requests bind UID and GID and a later identity change prevents delivery', function () {
    $operation = SubmitNodeOperation::run($this->actor, $this->context, $this->input);
    expect($operation->request['protocol'])->toBe(2)
        ->and($operation->request['execution_uid'])->toBe(1001)->and($operation->request['execution_gid'])->toBe(1001);
    $this->context->update(['execution_uid' => 1002]);
    $this->transport->shouldNotReceive('execute');
    (new ExecuteNodeOperationJob($operation->id, $this->actor->id))->handle($this->transport);
    expect($operation->fresh()->status)->toBe('denied')->and($operation->fresh()->error_code)->toBe('transport_changed');
});

test('stage journals a canonical spec and deduplicates equivalent input without a spec column', function () {
    $spec = json_decode(file_get_contents(base_path('tests/Fixtures/quadlet/prebuilt-v1/spec.json')), true);
    $input = [...$this->input, 'action' => 'stage', 'spec' => $spec];
    $first = SubmitNodeOperation::run($this->actor, $this->context, $input);
    unset($spec['profile'], $spec['health'], $spec['limits']);
    $spec['ports'][0]['host'] = (string) $spec['ports'][0]['host'];
    $again = SubmitNodeOperation::run($this->actor, $this->context, [...$input, 'spec' => $spec]);
    expect($first->uuid)->toBe($again->uuid)->and(NodeOperation::count())->toBe(1)
        ->and($again->request)->toBe($first->request)
        ->and($first->request['spec'])->toBe(ApplicationSpec::fromArray($spec)->toArray())
        ->and($first->getAttributes())->not->toHaveKey('spec');
    Queue::assertPushed(ExecuteNodeOperationJob::class);
});

test('stage idempotency cannot change the canonical spec', function () {
    $spec = json_decode(file_get_contents(base_path('tests/Fixtures/quadlet/prebuilt-v1/spec.json')), true);
    $input = [...$this->input, 'action' => 'stage', 'spec' => $spec];
    $first = SubmitNodeOperation::run($this->actor, $this->context, $input);
    $spec['command'][] = 'different';
    try {
        SubmitNodeOperation::run($this->actor, $this->context, [...$input, 'spec' => $spec]);
        test()->fail('Changed staging intent was accepted');
    } catch (HttpException $exception) {
        expect($exception->getStatusCode())->toBe(409);
    }
    expect(NodeOperation::count())->toBe(1)->and($first->fresh()->request)->toBe($first->request);
});

test('stage API requires current administration and a scoped writable token', function () {
    $spec = json_decode(file_get_contents(base_path('tests/Fixtures/quadlet/prebuilt-v1/spec.json')), true);
    $input = [...$this->input, 'action' => 'stage', 'spec' => $spec];
    $url = "/api/v1/servers/{$this->server->uuid}/runtime-contexts/{$this->context->uuid}/operations";
    $readToken = $this->actor->createToken('read', ['read'])->plainTextToken;
    $writeToken = $this->actor->createToken('write', ['read', 'write'])->plainTextToken;
    $this->withToken($readToken)->postJson($url, $input)->assertForbidden();
    $this->app['auth']->forgetGuards();
    $this->withToken($writeToken)->postJson($url, $input)->assertAccepted()->assertJsonPath('action', 'stage');
    $foreign = RuntimeContext::factory()->create();
    $this->withToken($writeToken)->postJson(str_replace($this->context->uuid, $foreign->uuid, $url), $input)->assertNotFound();
    $this->team->members()->updateExistingPivot($this->actor->id, ['role' => 'member']);
    $this->withToken($writeToken)->postJson($url, $input)->assertForbidden();
    expect(NodeOperation::count())->toBe(1);
});

test('stage API rejects raw units paths missing specs and specs on other actions', function (string $invalid) {
    $spec = json_decode(file_get_contents(base_path('tests/Fixtures/quadlet/prebuilt-v1/spec.json')), true);
    $input = [...$this->input, 'action' => 'stage', 'spec' => $spec];
    match ($invalid) {
        'raw units' => $input['spec']['units'] = ['app.container' => '[Container]'],
        'host path' => $input['spec']['volumes'][0]['source'] = '/etc',
        'missing' => $input = array_diff_key($input, ['spec' => true]),
        'other action' => $input = [...$input, 'action' => 'status', 'spec' => []],
    };
    $token = $this->actor->createToken('node', ['read', 'write'])->plainTextToken;
    $url = "/api/v1/servers/{$this->server->uuid}/runtime-contexts/{$this->context->uuid}/operations";
    $this->withToken($token)->postJson($url, $input)->assertUnprocessable();
    expect(NodeOperation::count())->toBe(0);
    Queue::assertNothingPushed();
})->with(['raw units', 'host path', 'missing', 'other action']);

test('stage rejects a normalized request exceeding the node byte budget before persistence', function () {
    $spec = json_decode(file_get_contents(base_path('tests/Fixtures/quadlet/prebuilt-v1/spec.json')), true);
    $spec['command'] = array_fill(0, 4, str_repeat('é', 1024));
    try {
        SubmitNodeOperation::run($this->actor, $this->context, [...$this->input, 'action' => 'stage', 'spec' => $spec]);
        test()->fail('Oversized request was accepted');
    } catch (ValidationException $exception) {
        expect($exception->errors())->toHaveKey('operation');
    }
    expect(NodeOperation::count())->toBe(0);
    Queue::assertNothingPushed();
});

test('stage reconciliation preserves the same canonical payload after a lost reply', function () {
    $spec = json_decode(file_get_contents(base_path('tests/Fixtures/quadlet/prebuilt-v1/spec.json')), true);
    $operation = SubmitNodeOperation::run($this->actor, $this->context, [...$this->input, 'action' => 'stage', 'spec' => $spec]);
    $original = $operation->request;
    $this->transport->shouldReceive('execute')->once()->andThrow(new RuntimeException('Lost staging reply'));
    (new ExecuteNodeOperationJob($operation->id, $this->actor->id))->handle($this->transport);
    expect($operation->fresh()->status)->toBe('uncertain')->and($operation->fresh()->request)->toBe($original);
    $this->transport->shouldReceive('execute')->once()->withArgs(fn ($context, $request) => $request === $original)
        ->andReturn(['status' => 'succeeded', 'operation_id' => $operation->uuid, 'resource_id' => $operation->resource_id,
            'generation' => $operation->expected_generation, 'bundle' => ['state' => 'staged']]);
    (new ExecuteNodeOperationJob($operation->id, $this->actor->id))->handle($this->transport);
    expect($operation->fresh()->status)->toBe('succeeded')->and($operation->fresh()->result['bundle']['state'])->toBe('staged')
        ->and($operation->fresh()->request)->toBe($original)->and($operation->fresh()->attempts)->toBe(2);
});

function stagedNodeOutcome(NodeOperation $operation): array
{
    $spec = ApplicationSpec::fromArray($operation->request['spec']);
    $files = app(QuadletCompiler::class)->compile($spec, $operation->resource_id);

    return ['status' => 'succeeded', 'operation_id' => $operation->uuid, 'resource_id' => $operation->resource_id,
        'generation' => $operation->expected_generation, 'bundle' => ['state' => 'staged', 'resource_id' => $operation->resource_id,
            'compiler_version' => QuadletCompiler::VERSION, 'spec_hash' => $spec->hash(),
            'bundle_hash' => hash('sha256', json_encode($files, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_LINE_TERMINATORS))]];
}

test('staged outbox success validates the expected bundle before adopting the operation', function () {
    $spec = json_decode(file_get_contents(base_path('tests/Fixtures/quadlet/prebuilt-v1/spec.json')), true);
    $operation = SubmitNodeOperation::run($this->actor, $this->context, [...$this->input, 'action' => 'stage', 'spec' => $spec]);
    $outcome = stagedNodeOutcome($operation);
    $event = ['sequence' => 1, 'resource_id' => $operation->resource_id, 'payload' => $outcome];
    ReceiveNodeEvents::run($this->actor, $this->context, $operation->resource_id, [$event]);
    ReceiveNodeEvents::run($this->actor, $this->context, $operation->resource_id, [$event]);
    expect(NodeEvent::count())->toBe(1)->and($operation->fresh()->result)->toBe($outcome)
        ->and($operation->fresh()->status)->toBe('succeeded');
});

test('staged outbox success cannot bypass bundle validation and rolls the page back', function (string $field, mixed $value) {
    $spec = json_decode(file_get_contents(base_path('tests/Fixtures/quadlet/prebuilt-v1/spec.json')), true);
    $operation = SubmitNodeOperation::run($this->actor, $this->context, [...$this->input, 'action' => 'stage', 'spec' => $spec]);
    $outcome = stagedNodeOutcome($operation);
    data_set($outcome, $field, $value);
    $events = [['sequence' => 1, 'resource_id' => $operation->resource_id,
        'payload' => ['operation_id' => $operation->uuid, 'resource_id' => $operation->resource_id, 'status' => 'needs_intervention', 'code' => 'interrupted']],
        ['sequence' => 2, 'resource_id' => $operation->resource_id, 'payload' => $outcome]];
    expect(fn () => ReceiveNodeEvents::run($this->actor, $this->context, $operation->resource_id, $events))->toThrow(RuntimeException::class);
    expect(NodeEvent::count())->toBe(0)->and($operation->fresh()->status)->toBe('queued')->and($operation->fresh()->result)->toBeNull();
})->with([['bundle.state', 'active'], ['bundle.spec_hash', str_repeat('0', 64)], ['bundle.bundle_hash', str_repeat('0', 64)], ['generation', 99]]);

test('recovery adoption cannot bypass validation of a staged target outcome', function () {
    $spec = json_decode(file_get_contents(base_path('tests/Fixtures/quadlet/prebuilt-v1/spec.json')), true);
    $target = SubmitNodeOperation::run($this->actor, $this->context, [...$this->input, 'action' => 'stage', 'spec' => $spec]);
    $target->update(['status' => 'uncertain']);
    $operation = SubmitNodeOperation::run($this->actor, $this->context, [...$this->input,
        'idempotency_key' => (string) Str::uuid(), 'action' => 'recover', 'target_operation_id' => $target->uuid]);
    $outcome = stagedNodeOutcome($target);
    $outcome['bundle']['state'] = 'active';
    $this->transport->shouldReceive('execute')->once()->andReturn([
        'status' => 'succeeded', 'operation_id' => $operation->uuid, 'resource_id' => $target->resource_id,
        'generation' => $operation->expected_generation, 'recovered_operation' => $outcome,
    ]);
    (new ExecuteNodeOperationJob($operation->id, $this->actor->id))->handle($this->transport);
    expect($operation->fresh()->status)->toBe('needs_intervention')->and($operation->fresh()->error_code)->toBe('outcome_publication_failed')
        ->and($target->fresh()->status)->toBe('uncertain')->and($target->fresh()->result)->toBeNull();
});

test('activation submission persists only a staged hash and deduplicates immutable release intent', function () {
    $input = [...$this->input, 'action' => 'activate', 'bundle_hash' => str_repeat('a', 64)];
    $first = SubmitNodeOperation::run($this->actor, $this->context, $input);
    expect($first->request['bundle_hash'])->toBe($input['bundle_hash'])
        ->and(SubmitNodeOperation::run($this->actor, $this->context, $input)->uuid)->toBe($first->uuid);
    expect(fn () => SubmitNodeOperation::run($this->actor, $this->context, [...$input, 'bundle_hash' => str_repeat('b', 64)]))
        ->toThrow(HttpException::class);
});

test('activation API rejects members foreign contexts raw units and invalid hashes', function () {
    $token = $this->actor->createToken('activation', ['read', 'write'])->plainTextToken;
    $url = "/api/v1/servers/{$this->server->uuid}/runtime-contexts/{$this->context->uuid}/operations";
    $input = [...$this->input, 'action' => 'activate', 'bundle_hash' => str_repeat('a', 64)];
    $this->withToken($token)->postJson($url, [...$input, 'bundle_hash' => '../foreign'])->assertUnprocessable();
    $this->withToken($token)->postJson($url, [...$input, 'unit' => 'arbitrary.service'])->assertUnprocessable();
    $this->withToken($token)->postJson($url, [...$input, 'spec' => []])->assertUnprocessable();
    $foreign = RuntimeContext::factory()->create();
    $this->withToken($token)->postJson(str_replace($this->context->uuid, $foreign->uuid, $url), $input)->assertNotFound();
    $this->withToken($token)->postJson($url, $input)->assertAccepted()->assertJsonPath('action', 'activate')->assertJsonMissingPath('request');
    $this->team->members()->updateExistingPivot($this->actor->id, ['role' => 'member']);
    $this->withToken($token)->postJson($url, [...$input, 'idempotency_key' => (string) Str::uuid()])->assertForbidden();
    expect(NodeOperation::count())->toBe(1);
});

function nodeActivationEventOutcome(NodeOperation $operation, string $status): array
{
    $hash = $operation->request['bundle_hash'];
    $reply = ['status' => $status, 'operation_id' => $operation->uuid, 'resource_id' => $operation->resource_id,
        'generation' => $operation->expected_generation + (int) ($status !== 'executing'),
        'release' => ['state' => $status === 'executing' ? 'activating' : 'rolled_back',
            'bundle_hash' => $hash, 'previous_bundle_hash' => str_repeat('b', 64)]];
    if ($status === 'failed') {
        $reply['release']['data_recovery'] = false;
        $reply['observed'] = ['active' => true, 'health' => true, 'bundle_hash' => str_repeat('b', 64)];
    }

    return $reply;
}

test('activation events adopt validated running and compensated phases without regressing terminal state', function () {
    $operation = SubmitNodeOperation::run($this->actor, $this->context, [...$this->input, 'action' => 'activate', 'bundle_hash' => str_repeat('a', 64)]);
    $event = fn (int $sequence, string $status) => ['sequence' => $sequence, 'resource_id' => $operation->resource_id,
        'payload' => nodeActivationEventOutcome($operation, $status)];
    ReceiveNodeEvents::run($this->actor, $this->context, $operation->resource_id, [$event(10, 'executing')]);
    expect($operation->fresh()->status)->toBe('executing');
    ReceiveNodeEvents::run($this->actor, $this->context, $operation->resource_id, [$event(11, 'failed'), $event(9, 'executing')]);
    expect($operation->fresh()->status)->toBe('failed')->and($operation->fresh()->isTerminal())->toBeTrue()
        ->and($operation->fresh()->last_node_sequence)->toBe(11);
});

test('forged activation event cannot advance journal or persist an event', function () {
    $operation = SubmitNodeOperation::run($this->actor, $this->context, [...$this->input, 'action' => 'activate', 'bundle_hash' => str_repeat('a', 64)]);
    $payload = nodeActivationEventOutcome($operation, 'failed');
    $payload['release']['data_recovery'] = true;
    expect(fn () => ReceiveNodeEvents::run($this->actor, $this->context, $operation->resource_id,
        [['sequence' => 1, 'resource_id' => $operation->resource_id, 'payload' => $payload]]))->toThrow(RuntimeException::class);
    expect(NodeEvent::count())->toBe(0)->and($operation->fresh()->status)->toBe('queued');
});

test('scheduler continues validated running activation past the transport retry cap', function () {
    $operation = SubmitNodeOperation::run($this->actor, $this->context, [...$this->input, 'action' => 'activate', 'bundle_hash' => str_repeat('a', 64)]);
    Queue::fake();
    $operation->update(['status' => 'executing', 'attempts' => 8, 'result' => nodeActivationEventOutcome($operation, 'executing')]);
    $this->artisan('node:reconcile')->assertSuccessful();
    Queue::assertPushed(ExecuteNodeOperationJob::class, 1);
    Queue::fake();
    $operation->update(['status' => 'uncertain']);
    $this->artisan('node:reconcile')->assertSuccessful();
    Queue::assertNothingPushed();
});

test('recovery adopts validated failed activation and prevents further execution of its compensated intent', function () {
    $target = SubmitNodeOperation::run($this->actor, $this->context, [...$this->input, 'action' => 'activate', 'bundle_hash' => str_repeat('a', 64)]);
    $target->update(['status' => 'uncertain']);
    $operation = SubmitNodeOperation::run($this->actor, $this->context, [...$this->input, 'action' => 'recover',
        'idempotency_key' => (string) Str::uuid(), 'target_operation_id' => $target->uuid]);
    $recovered = nodeActivationEventOutcome($target, 'failed');
    $this->transport->shouldReceive('execute')->once()->andReturn(['status' => 'succeeded', 'operation_id' => $operation->uuid,
        'resource_id' => $target->resource_id, 'generation' => $recovered['generation'], 'recovered_operation' => $recovered]);
    (new ExecuteNodeOperationJob($operation->id, $this->actor->id))->handle($this->transport);
    (new ExecuteNodeOperationJob($target->id, $this->actor->id))->handle($this->transport);
    expect($target->fresh()->status)->toBe('failed')->and($target->fresh()->isTerminal())->toBeTrue()
        ->and($target->fresh()->result)->toBe($recovered);
});
