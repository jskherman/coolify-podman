<?php

namespace App\Http\Controllers\Api;

use App\Actions\Server\ProbeRuntimeCapabilities;
use App\Exceptions\RuntimeCapabilityProbeFailed;
use App\Http\Controllers\Controller;
use App\Models\RuntimeContext;
use App\Models\Server;
use Illuminate\Http\JsonResponse;
use Illuminate\Http\Request;
use OpenApi\Attributes as OA;

class RuntimeCapabilitiesController extends Controller
{
    #[OA\Get(
        path: '/servers/{uuid}/runtime-contexts', operationId: 'list-runtime-contexts',
        summary: 'List current identity capability observations', tags: ['Servers'],
        security: [['bearerAuth' => []]],
        parameters: [new OA\Parameter(name: 'uuid', in: 'path', required: true, schema: new OA\Schema(type: 'string'))],
        responses: [new OA\Response(response: 200, description: 'Current SSH identity runtime observations; prerequisites do not imply qualified deployment support.'), new OA\Response(response: 403, description: 'Server administrator required.'), new OA\Response(response: 404, description: 'Server not found.')],
    )]
    public function index(Request $request): JsonResponse
    {
        $server = $this->server($request);

        return response()->json(RuntimeContext::query()->where('server_id', $server->id)
            ->where('connection_fingerprint', RuntimeContext::connectionFingerprint($server))
            ->orderByDesc('probed_at')->get());
    }

    #[OA\Post(
        path: '/servers/{uuid}/runtime-contexts/probe', operationId: 'probe-runtime-context',
        summary: 'Observe and persist capabilities as the configured SSH identity', tags: ['Servers'],
        security: [['bearerAuth' => []]],
        parameters: [new OA\Parameter(name: 'uuid', in: 'path', required: true, schema: new OA\Schema(type: 'string'))],
        responses: [new OA\Response(response: 200, description: 'Observed runtime context.'), new OA\Response(response: 403, description: 'Server administrator and write token required.'), new OA\Response(response: 404, description: 'Server not found.'), new OA\Response(response: 409, description: 'Server identity changed during observation.')],
    )]
    public function probe(Request $request): JsonResponse
    {
        try {
            return response()->json(ProbeRuntimeCapabilities::run($request->user(), $this->server($request)));
        } catch (RuntimeCapabilityProbeFailed $exception) {
            return response()->json(['code' => 'probe_unavailable', 'message' => $exception->getMessage()], 422);
        }
    }

    private function server(Request $request): Server
    {
        $teamId = getTeamIdFromToken();
        abort_if($teamId === null, 401);
        $server = Server::query()->where('team_id', $teamId)->where('uuid', $request->uuid)->firstOrFail();
        $this->authorize('manageRuntime', $server);

        return $server;
    }
}
