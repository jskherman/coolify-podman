<?php

namespace App\Services;

use RuntimeException;

class NodeActivationValidator
{
    /** @param array<string, mixed> $request */
    public function validateRequest(array $request): void
    {
        if (($request['protocol'] ?? null) !== 2 || ($request['action'] ?? null) !== 'activate'
            || ! $this->isHash($request['bundle_hash'] ?? null)
            || ! is_int($request['expected_generation'] ?? null) || $request['expected_generation'] < 0) {
            throw new RuntimeException('Native activation requires protocol 2, a staged bundle hash and an exact generation.');
        }
    }

    /** @param array<string, mixed> $request
     * @param  array<string, mixed>  $outcome
     */
    public function validateOutcome(array $request, array $outcome): void
    {
        $this->validateRequest($request);
        $status = $outcome['status'] ?? null;
        $release = $outcome['release'] ?? null;
        if (! in_array($status, ['executing', 'succeeded', 'failed'], true)
            || ($outcome['operation_id'] ?? null) !== $request['operation_id']
            || ($outcome['resource_id'] ?? null) !== $request['resource_id']
            || ! is_array($release) || ! array_key_exists('previous_bundle_hash', $release)
            || ($release['bundle_hash'] ?? null) !== $request['bundle_hash']
            || ($release['previous_bundle_hash'] !== null && ! $this->isHash($release['previous_bundle_hash']))) {
            throw new RuntimeException('Activation reply does not match the authorized release.');
        }
        $fields = ['state', 'bundle_hash', 'previous_bundle_hash', ...match ($status) {
            'succeeded' => ['no_op'], 'failed' => ['data_recovery'], default => [],
        }];
        if (array_diff(array_keys($release), $fields) || array_diff($fields, array_keys($release))) {
            throw new RuntimeException('Activation reply contains unsupported release fields.');
        }
        if ($status === 'executing') {
            if (! in_array($release['state'], ['activating', 'compensating'], true)
                || ($outcome['generation'] ?? null) !== $request['expected_generation']) {
                throw new RuntimeException('Running activation has an invalid phase or generation.');
            }

            return;
        }
        $observed = $outcome['observed'] ?? null;
        if (! is_array($observed) || ! is_bool($observed['active'] ?? null) || ! is_bool($observed['health'] ?? null)) {
            throw new RuntimeException('Terminal activation lacks a valid runtime observation.');
        }
        if ($status === 'succeeded') {
            if ($release['state'] !== 'committed' || ! is_bool($release['no_op'])
                || ! $observed['active'] || ! $observed['health']
                || ($observed['bundle_hash'] ?? null) !== $request['bundle_hash']
                || ($release['no_op'] && $release['previous_bundle_hash'] !== $request['bundle_hash'])
                || ($outcome['generation'] ?? null) !== $request['expected_generation'] + (int) ! $release['no_op']) {
                throw new RuntimeException('Committed activation lacks the expected health, artifact or generation.');
            }
        } elseif ($release['state'] !== 'rolled_back' || $release['data_recovery'] !== false
            || ! array_key_exists('bundle_hash', $observed) || $observed['bundle_hash'] !== $release['previous_bundle_hash']
            || ($outcome['generation'] ?? null) !== $request['expected_generation'] + 1) {
            throw new RuntimeException('Compensation must identify the previous application and cannot claim data recovery.');
        }
    }

    private function isHash(mixed $value): bool
    {
        return is_string($value) && preg_match('/\A[a-f0-9]{64}\z/', $value) === 1;
    }
}
