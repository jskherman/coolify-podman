<?php

use App\Data\ApplicationSpec;
use App\Services\QuadletCompiler;
use Illuminate\Support\Facades\Process;

beforeEach(function () {
    if (getenv('COOLIFY_RUNTIME_VM_TEST') !== '1') {
        $this->markTestSkipped('Requires the disposable Podman 5.4.2 VM.');
    }
});

test('the target rootless generator accepts the complete compiled bundle', function () {
    $directory = storage_path('app/runtime-test-vm');
    $state = json_decode(file_get_contents($directory.'/state.json'), true, flags: JSON_THROW_ON_ERROR);
    expect($state['stage'])->toBe('running');
    $spec = ApplicationSpec::fromArray(json_decode(file_get_contents(base_path('tests/Fixtures/quadlet/prebuilt-v1/spec.json')), true, flags: JSON_THROW_ON_ERROR));
    $files = app(QuadletCompiler::class)->compile($spec, '11111111-1111-4111-8111-111111111111');
    $script = file_get_contents(base_path('tests/Fixtures/validate-quadlet-bundle.py'));
    $result = Process::input(json_encode(['fixture' => $state['operation_id'], 'files' => $files], JSON_THROW_ON_ERROR))
        ->timeout(45)->run(['ssh', '-F', '/dev/null', '-T', '-i', $directory.'/id_ed25519', '-p', (string) $state['ssh_port'],
            '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'UserKnownHostsFile='.$directory.'/known_hosts', 'workload@127.0.0.1', 'python3 -c '.escapeshellarg($script)]);
    expect($result->exitCode())->toBe(0, $result->errorOutput());
    $report = json_decode($result->output(), true, flags: JSON_THROW_ON_ERROR);
    expect($report['code'])->toBe(0, $report['errors']);
    foreach (['coolify-11111111-1111-4111-8111-111111111111.service',
        'coolify-11111111-1111-4111-8111-111111111111-network.service',
        'coolify-11111111-1111-4111-8111-111111111111-data-volume.service'] as $unit) {
        expect($report['output'])->toContain($unit);
    }
    expect($report['output'])->toContain('--internal', '127.0.0.1:18100:80', 'MemoryMax=128M', 'CPUQuota=100%');
})->group('runtime-vm');

test('the generated units preserve container arguments labels dependencies and effective limits', function () {
    $directory = storage_path('app/runtime-test-vm');
    $state = json_decode(file_get_contents($directory.'/state.json'), true, flags: JSON_THROW_ON_ERROR);
    expect($state['stage'])->toBe('running');
    $resourceId = '9c750b0e-4a61-4e6a-a465-21f59ec073a5';
    $name = 'coolify-'.$resourceId;
    $input = json_decode(file_get_contents(base_path('tests/Fixtures/quadlet/prebuilt-v1/spec.json')), true, flags: JSON_THROW_ON_ERROR);
    $input['entrypoint'] = ['sh', '-c', 'exec sleep 3600', 'compiler-fixture'];
    $input['command'] = ['snowman ☃ café', 'double "quote"', "single 'quote'", 'back\\slash'];
    $input['labels'] = [['name' => 'fixture.value', 'value' => 'Unicode café ☃ "quotes" back\\slash']];
    $input['ports'][0]['host'] = 18101;
    $input['limits'] = ['memory_mib' => 129, 'cpu_millis' => 1255];
    $specification = ApplicationSpec::fromArray($input);
    $files = app(QuadletCompiler::class)->compile($specification, $resourceId);
    $bundleHash = hash('sha256', json_encode($files, JSON_THROW_ON_ERROR | JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_LINE_TERMINATORS));
    $script = file_get_contents(base_path('tests/Fixtures/activate-quadlet-compiler-fixture.py'));
    $request = ['fixture' => $state['operation_id'], 'resource_id' => $resourceId,
        'spec_hash' => $specification->hash(), 'bundle_hash' => $bundleHash, 'files' => $files];
    $reports = [];
    for ($attempt = 0; $attempt < 2; $attempt++) {
        $result = Process::input(json_encode($request, JSON_THROW_ON_ERROR))->timeout(120)->run([
            'ssh', '-F', '/dev/null', '-T', '-i', $directory.'/id_ed25519', '-p', (string) $state['ssh_port'],
            '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'UserKnownHostsFile='.$directory.'/known_hosts', 'workload@127.0.0.1', 'python3 -c '.escapeshellarg($script),
        ]);
        expect($result->exitCode())->toBe(0, $result->errorOutput());
        $reports[] = json_decode($result->output(), true, flags: JSON_THROW_ON_ERROR);
    }
    $report = $reports[0];
    expect($reports[1])->toBe($report)
        ->and($report['resource_id'])->toBe($resourceId)->and($report['bundle_hash'])->toBe($bundleHash)
        ->and($report['spec_hash'])->toBe($specification->hash())->and($report['image'])->toBe($input['image'])
        ->and($report['entrypoint'])->toBe($input['entrypoint'])->and($report['command'])->toBe($input['command'])
        ->and($report['labels']['fixture.value'])->toBe($input['labels'][0]['value'])
        ->and($report['labels']['coolify.resource'])->toBe($resourceId)
        ->and($report['labels']['coolify.managed'])->toBe('true')
        ->and($report['restart_policy'])->toBeIn(['', 'no'])
        ->and($report['systemd']['InvocationID'])->toMatch('/\A[a-f0-9]{32}\z/')
        ->and($report['systemd']['ActiveState'])->toBe('active')
        ->and($report['systemd']['MemoryMax'])->toBe((string) (129 * 1024 * 1024))
        ->and($report['systemd']['CPUQuotaPerSecUSec'])->toBe('1.255000s');
    foreach (['Requires', 'After'] as $property) {
        expect(explode(' ', $report['systemd'][$property]))->toContain($name.'-network.service', $name.'-data-volume.service');
    }
    $volume = collect($report['mounts'])->firstWhere('Destination', '/data');
    expect($volume['Type'])->toBe('volume')->and($volume['Name'])->toBe($name.'-data')->and($volume['RW'])->toBeTrue();
})->group('runtime-vm');
