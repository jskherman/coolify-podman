<?php

use App\Services\NodeActivationValidator;

function activationRequest(): array
{
    return ['protocol' => 2, 'action' => 'activate', 'operation_id' => '11111111-1111-4111-8111-111111111111',
        'resource_id' => '22222222-2222-4222-8222-222222222222', 'expected_generation' => 2, 'bundle_hash' => str_repeat('a', 64)];
}

function activationOutcome(string $status = 'succeeded', bool $noOp = false): array
{
    $request = activationRequest();
    $result = ['status' => $status, 'operation_id' => $request['operation_id'], 'resource_id' => $request['resource_id'],
        'generation' => 2 + (int) ($status !== 'executing' && ! $noOp),
        'release' => ['state' => match ($status) {
            'succeeded' => 'committed', 'failed' => 'rolled_back', default => 'activating'
        },
            'bundle_hash' => $request['bundle_hash'], 'previous_bundle_hash' => $noOp ? $request['bundle_hash'] : str_repeat('b', 64)],
        'observed' => ['active' => true, 'health' => true, 'bundle_hash' => $status === 'failed' ? str_repeat('b', 64) : $request['bundle_hash'],
            'invocation' => str_repeat('c', 32), 'transitioning' => false, 'failed' => false]];
    if ($status === 'succeeded') {
        $result['release']['no_op'] = $noOp;
    } elseif ($status === 'failed') {
        $result['release']['data_recovery'] = false;
    } else {
        unset($result['observed']);
    }

    return $result;
}

test('activation validates committed no-op running and compensated outcomes', function (string $status, bool $noOp) {
    app(NodeActivationValidator::class)->validateOutcome(activationRequest(), activationOutcome($status, $noOp));
    expect(true)->toBeTrue();
})->with([['succeeded', false], ['succeeded', true], ['executing', false], ['failed', false]]);

test('activation rejects substituted artifacts generations and false recovery claims', function (string $field, mixed $value, string $status) {
    $reply = activationOutcome($status);
    data_set($reply, $field, $value);
    app(NodeActivationValidator::class)->validateOutcome(activationRequest(), $reply);
})->with([
    ['operation_id', 'foreign', 'succeeded'], ['resource_id', 'foreign', 'succeeded'],
    ['release.bundle_hash', str_repeat('f', 64), 'succeeded'], ['generation', 4, 'succeeded'],
    ['release.no_op', 'yes', 'succeeded'], ['observed.health', false, 'succeeded'],
    ['observed.active', false, 'succeeded'], ['observed.bundle_hash', str_repeat('b', 64), 'succeeded'],
    ['release.state', 'committed', 'failed'], ['release.data_recovery', true, 'failed'],
    ['generation', 2, 'failed'], ['generation', 3, 'executing'],
    ['release.state', 'committed', 'executing'], ['release.raw_admin', 'any', 'executing'],
])->throws(RuntimeException::class);
