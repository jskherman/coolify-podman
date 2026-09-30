<?php

namespace App\Http\Controllers\Api;

use App\Actions\Server\ConfigureNodeExecutor;
use App\Actions\Server\SubmitNodeOperation;
use App\Http\Controllers\Controller;
use App\Jobs\ExecuteNodeOperationJob;
use App\Models\NodeEvent;
use App\Models\NodeOperation;
use App\Models\RuntimeContext;
use App\Models\Server;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use OpenApi\Attributes as OA;

class NodeOperationsController extends Controller
{
    #[OA\Patch(path: '/servers/{uuid}/runtime-contexts/{context_uuid}/executor', operationId: 'configure-node-executor', summary: 'Pin a dedicated restricted executor credential and node authority versions', tags: ['Servers'], security: [['bearerAuth' => []]], parameters: [new OA\Parameter(name: 'uuid', in: 'path', required: true, schema: new OA\Schema(type: 'string')), new OA\Parameter(name: 'context_uuid', in: 'path', required: true, schema: new OA\Schema(type: 'string'))], requestBody: new OA\RequestBody(required: true, content: new OA\JsonContent(required: ['private_key_uuid', 'ssh_host_key', 'controller_epoch', 'policy_version'], properties: [new OA\Property(property: 'private_key_uuid', type: 'string'), new OA\Property(property: 'ssh_host_key', type: 'string'), new OA\Property(property: 'controller_epoch', type: 'integer', minimum: 1), new OA\Property(property: 'policy_version', type: 'integer', minimum: 1)])), responses: [new OA\Response(response: 200, description: 'Connection configuration saved; does not install policy or transfer ownership.'), new OA\Response(response: 403, description: 'Current team administrator required.'), new OA\Response(response: 422, description: 'Unsupported identity, credential or host key.')])]
    public function configure(Request $request): JsonResponse
    {
        return response()->json(ConfigureNodeExecutor::run($request->user(), $this->context($request), $request->all()));
    }

    #[OA\Get(path: '/servers/{uuid}/runtime-contexts/{context_uuid}/operations', operationId: 'list-node-operations', summary: 'Read the latest 50 scoped operation outcomes', tags: ['Servers'], security: [['bearerAuth' => []]], parameters: [new OA\Parameter(name: 'uuid', in: 'path', required: true, schema: new OA\Schema(type: 'string')), new OA\Parameter(name: 'context_uuid', in: 'path', required: true, schema: new OA\Schema(type: 'string'))], responses: [new OA\Response(response: 200, description: 'Persisted operations, newest first.'), new OA\Response(response: 403, description: 'Current team administrator required.')])]
    public function index(Request $request): JsonResponse
    {
        $context = $this->context($request);

        return response()->json(NodeOperation::query()->where('runtime_context_id', $context->id)->latest('id')->limit(50)->get());
    }

    #[OA\Get(path: '/servers/{uuid}/runtime-contexts/{context_uuid}/events', operationId: 'list-node-events', summary: 'Read persisted scoped node observations', tags: ['Servers'], security: [['bearerAuth' => []]], parameters: [new OA\Parameter(name: 'uuid', in: 'path', required: true, schema: new OA\Schema(type: 'string')), new OA\Parameter(name: 'context_uuid', in: 'path', required: true, schema: new OA\Schema(type: 'string'))], responses: [new OA\Response(response: 200, description: 'Latest 50 ingested node events.'), new OA\Response(response: 403, description: 'Current team administrator required.')])]
    public function events(Request $request): JsonResponse
    {
        $context = $this->context($request);

        return response()->json(NodeEvent::query()->where('runtime_context_id', $context->id)->orderByDesc('sequence')->limit(50)->get());
    }

    #[OA\Post(path: '/servers/{uuid}/runtime-contexts/{context_uuid}/operations', operationId: 'submit-node-operation', summary: 'Journal a typed, generation-fenced lifecycle, staging or native activation operation', tags: ['Servers'], security: [['bearerAuth' => []]], parameters: [new OA\Parameter(name: 'uuid', in: 'path', required: true, schema: new OA\Schema(type: 'string')), new OA\Parameter(name: 'context_uuid', in: 'path', required: true, schema: new OA\Schema(type: 'string'))], requestBody: new OA\RequestBody(required: true, content: new OA\JsonContent(required: ['resource_id', 'action', 'expected_generation', 'idempotency_key'], properties: [new OA\Property(property: 'resource_id', type: 'string', format: 'uuid'), new OA\Property(property: 'action', type: 'string', enum: ['status', 'start', 'stop', 'restart', 'recover', 'events', 'stage', 'activate']), new OA\Property(property: 'bundle_hash', description: 'Required only for activate. Lowercase SHA256 of an existing staged bundle in the protected resource scope. Asynchronous health-gated activation exposes executing, committed, compensated-failure or needs-intervention results. Application compensation does not recover database state.', type: 'string', pattern: '^[a-f0-9]{64}$'), new OA\Property(property: 'spec', description: 'Required only for stage. Closed schema-version 1 prebuilt-v1 ApplicationSpec; server normalizes defaults. Accepts immutable image, command, entrypoint, health, bounded limits, internal network, one loopback port, named volumes and labels. Raw units, host paths and unknown keys are rejected. Stages a validated bundle without activation or generation changes. The full encoded node request is limited to 16384 bytes.', type: 'object'), new OA\Property(property: 'cursor', description: 'Required only for events; last received node event sequence, or zero.', type: 'integer', minimum: 0), new OA\Property(property: 'expected_generation', type: 'integer', minimum: 0), new OA\Property(property: 'idempotency_key', type: 'string', format: 'uuid'), new OA\Property(property: 'target_operation_id', description: 'Required only for recover; identifies an earlier lifecycle intent in the same resource scope.', type: 'string', format: 'uuid')])), responses: [new OA\Response(response: 202, description: 'Intent persisted; asynchronous execution or reconciliation queued.'), new OA\Response(response: 403, description: 'Current team administrator required.'), new OA\Response(response: 409, description: 'Identity changed or idempotency key conflicts.'), new OA\Response(response: 422, description: 'Unsupported operation/specification fields or encoded request exceeds 16384 bytes.')])]
    public function store(Request $request): JsonResponse
    {
        return response()->json(SubmitNodeOperation::run($request->user(), $this->context($request), $request->all()), 202);
    }

    #[OA\Post(path: '/servers/{uuid}/runtime-contexts/{context_uuid}/operations/{operation_uuid}/reconcile', operationId: 'reconcile-node-operation', summary: 'Redeliver the original immutable request for observation and recovery', tags: ['Servers'], security: [['bearerAuth' => []]], parameters: [new OA\Parameter(name: 'uuid', in: 'path', required: true, schema: new OA\Schema(type: 'string')), new OA\Parameter(name: 'context_uuid', in: 'path', required: true, schema: new OA\Schema(type: 'string')), new OA\Parameter(name: 'operation_uuid', in: 'path', required: true, schema: new OA\Schema(type: 'string', format: 'uuid'))], responses: [new OA\Response(response: 202, description: 'Current operation state; unresolved intent queued without changing its ID.'), new OA\Response(response: 403, description: 'Current team administrator required.')])]
    public function reconcile(Request $request): JsonResponse
    {
        $context = $this->context($request);
        $operation = NodeOperation::query()->where('runtime_context_id', $context->id)->where('uuid', $request->operation_uuid)->firstOrFail();
        if (! $operation->isTerminal()) {
            ExecuteNodeOperationJob::dispatch($operation->id, $request->user()->id)->afterCommit();
        }

        return response()->json($operation, 202);
    }

    private function context(Request $request): RuntimeContext
    {
        $teamId = getTeamIdFromToken();
        abort_if($teamId === null, 401);
        $server = Server::query()->where('team_id', $teamId)->where('uuid', $request->uuid)->firstOrFail();
        $this->authorize('manageRuntime', $server);

        return RuntimeContext::query()->where('server_id', $server->id)->where('uuid', $request->context_uuid)->firstOrFail();
    }
}
