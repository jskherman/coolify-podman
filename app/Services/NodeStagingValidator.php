<?php

namespace App\Services;

use App\Data\ApplicationSpec;
use RuntimeException;

class NodeStagingValidator
{
    public function __construct(private QuadletCompiler $compiler) {}

    /** @param array<string, mixed> $request
     * @return array<string, string>
     */
    public function expectedBundle(array $request): array
    {
        if (($request['protocol'] ?? null) !== 2 || ($request['action'] ?? null) !== 'stage') {
            throw new RuntimeException('Bundle staging requires protocol 2.');
        }
        $spec = ApplicationSpec::fromArray($request['spec']);
        if ($spec->toArray() !== $request['spec']) {
            throw new RuntimeException('Bundle staging requires a canonical application specification.');
        }
        $files = $this->compiler->compile($spec, $request['resource_id']);

        return ['state' => 'staged', 'resource_id' => $request['resource_id'],
            'compiler_version' => QuadletCompiler::VERSION, 'spec_hash' => $spec->hash(),
            'bundle_hash' => hash('sha256', json_encode($files, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_LINE_TERMINATORS))];
    }

    /** @param array<string, mixed> $request
     * @param  array<string, mixed>  $outcome
     */
    public function validateOutcome(array $request, array $outcome): void
    {
        $expected = $this->expectedBundle($request);
        $bundle = $outcome['bundle'] ?? null;
        if (($outcome['status'] ?? null) !== 'succeeded'
            || ($outcome['operation_id'] ?? null) !== $request['operation_id']
            || ($outcome['resource_id'] ?? null) !== $request['resource_id']
            || ($outcome['generation'] ?? null) !== $request['expected_generation']
            || ! is_array($bundle) || array_diff(array_keys($bundle), [...array_keys($expected), 'validation'])) {
            throw new RuntimeException('Staging reply does not match the authorized bundle or generation.');
        }
        foreach ($expected as $field => $value) {
            if (($bundle[$field] ?? null) !== $value) {
                throw new RuntimeException('Staging reply does not match the authorized bundle or generation.');
            }
        }
        if (array_key_exists('validation', $bundle)) {
            $validation = $bundle['validation'];
            if (! is_array($validation) || count($validation) !== 2
                || ! is_string($validation['podman_version'] ?? null) || strlen($validation['podman_version']) > 64
                || ! preg_match('/\A[0-9]+\.[0-9]+\.[0-9]+(?:[+~-][A-Za-z0-9.-]+)?\z/', $validation['podman_version'])
                || ! is_string($validation['generator_sha256'] ?? null) || ! preg_match('/\A[a-f0-9]{64}\z/', $validation['generator_sha256'])) {
                throw new RuntimeException('Staging reply contains invalid generator validation evidence.');
            }
        }
    }
}
