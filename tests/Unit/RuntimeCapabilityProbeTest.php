<?php

use App\Services\RuntimeCapabilityProbe;
use Illuminate\Validation\ValidationException;
use Tests\TestCase;

uses(TestCase::class);

test('rejects a malformed observation instead of inferring native capabilities', function (array $changes) {
    $report = array_replace([
        'schema_version' => 1, 'runtime' => 'podman', 'uid' => 1001, 'gid' => 1001,
        'manager' => 'user', 'storage' => ['graph_root' => '/home/workload/storage', 'driver' => 'overlay'],
        'versions' => ['podman' => '5.4.2'],
        'capabilities' => ['runtime.podman' => ['available' => true, 'remediation' => null]],
    ], $changes);
    (new RuntimeCapabilityProbe)->parse(json_encode($report, JSON_THROW_ON_ERROR));
})->with([
    'unknown protocol' => [['schema_version' => 2]],
    'missing identity' => [['uid' => null]],
    'other runtime' => [['runtime' => 'docker']],
    'unknown manager' => [['manager' => 'automatic']],
    'unscoped storage attributes' => [['storage' => ['graph_root' => '/tmp', 'secret' => 'must not persist']]],
    'untyped availability' => [['capabilities' => ['runtime.podman' => ['available' => 'yes']]]],
])->throws(ValidationException::class);
