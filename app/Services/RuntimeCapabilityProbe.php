<?php

namespace App\Services;

use App\Exceptions\RuntimeCapabilityProbeFailed;
use App\Models\Server;
use Illuminate\Support\Facades\Validator;
use Throwable;

class RuntimeCapabilityProbe
{
    /** @return array<string, mixed> */
    public function inspect(Server $server): array
    {
        $script = file_get_contents(base_path('scripts/node-capability-probe.py'));
        try {
            $output = instant_remote_process(['python3 -c '.escapeshellarg($script)], $server, no_sudo: true, timeout: 180);
        } catch (Throwable $exception) {
            throw new RuntimeCapabilityProbeFailed('Probe unavailable. Verify SSH and Python 3 for this identity, then retry.', previous: $exception);
        }
        if ($output === null || strlen($output) > 32768) {
            throw new RuntimeCapabilityProbeFailed('Probe unavailable. Verify Python 3 and its output limit, then retry.');
        }
        try {
            return $this->parse($output);
        } catch (Throwable $exception) {
            throw new RuntimeCapabilityProbeFailed('Probe returned an invalid observation. Verify the installed probe protocol before retrying.', previous: $exception);
        }
    }

    /** @return array<string, mixed> */
    public function parse(string $output): array
    {
        $report = json_decode($output, true, 32, JSON_THROW_ON_ERROR);
        $validated = Validator::make($report, [
            'schema_version' => 'required|integer|in:1',
            'runtime' => 'required|in:podman',
            'uid' => 'required|integer|min:0',
            'gid' => 'required|integer|min:0',
            'manager' => 'required|in:user,system',
            'storage' => 'required|array:graph_root,driver',
            'storage.graph_root' => 'nullable|string|max:4096',
            'storage.driver' => 'nullable|string|max:100',
            'versions' => 'required|array',
            'versions.*' => 'nullable|string|max:500',
            'capabilities' => 'required|array',
            'capabilities.*' => 'required|array:available,remediation',
            'capabilities.*.available' => 'required|boolean',
            'capabilities.*.remediation' => 'nullable|string|max:1000',
        ])->validate();
        ksort($validated['capabilities']);
        ksort($validated['versions']);

        return $validated;
    }
}
