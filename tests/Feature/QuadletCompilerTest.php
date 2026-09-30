<?php

use App\Data\ApplicationSpec;
use App\Services\QuadletCompiler;
use Illuminate\Validation\ValidationException;

function prebuiltApplicationSpec(): array
{
    return ['schema_version' => 1,
        'image' => 'docker.io/library/busybox@sha256:5cec3fc171c87218698e85a52af7087de727372aae264a787b8112901a5b0092',
        'command' => ['httpd', '-f', '-p', '80', '-h', '/data'],
        'ports' => [['container' => 80, 'host' => 18100]],
        'volumes' => [['name' => 'data', 'target' => '/data']],
    ];
}

test('native spec normalizes deterministically while preserving command argument order', function () {
    $input = prebuiltApplicationSpec();
    $one = ApplicationSpec::fromArray($input);
    $two = ApplicationSpec::fromArray(array_reverse($input, true));
    expect($one->toArray())->toBe($two->toArray())->and($one->hash())->toBe($two->hash())
        ->and($one->toArray()['command'])->toBe($input['command'])
        ->and(ApplicationSpec::fromArray($one->toArray())->hash())->toBe($one->hash());
});

test('native profile refuses unsupported or unsafe configuration instead of dropping semantics', function (array $changes) {
    ApplicationSpec::fromArray(array_replace(prebuiltApplicationSpec(), $changes));
})->with([
    [['schema_version' => 2]], [['image' => 'busybox:latest']], [['privileged' => true]],
    [['command' => ["httpd\n[Service]\nExecStart=/bin/sh"]]], [['command' => ['$HOME']]],
    [['ports' => [['container' => 80, 'host' => 18100, 'bind' => '0.0.0.0']]]],
    [['volumes' => [['name' => 'data', 'target' => '/data', 'source' => '/etc']]]],
    [['volumes' => [['name' => 'data', 'target' => '/../etc']]]],
    [['network' => ['internal' => false]]], [['extensions' => ['PodmanArgs' => '--privileged']]],
    [['environment' => [['name' => 'PASSWORD', 'value' => 'plaintext']]]],
])->throws(ValidationException::class);

test('compiler renders owned container network and volume units with one lifecycle owner', function () {
    $resource = '11111111-1111-4111-8111-111111111111';
    $files = app(QuadletCompiler::class)->compile(ApplicationSpec::fromArray(prebuiltApplicationSpec()), $resource);
    expect(array_keys($files))->toBe([
        "coolify-$resource-data.volume", "coolify-$resource.container", "coolify-$resource.network",
    ]);
    expect($files["coolify-$resource.container"])
        ->toContain("# Coolify managed resource $resource\n", 'PublishPort=127.0.0.1:18100:80', 'Restart=always', 'WantedBy=default.target')
        ->toContain("Network=coolify-$resource.network", "Volume=coolify-$resource-data.volume:/data:rw")
        ->not->toContain('AutoUpdate=', 'PodmanArgs=', '--restart');
    expect($files["coolify-$resource.network"])->toContain('Internal=true');
    expect($files["coolify-$resource-data.volume"])->toContain("VolumeName=coolify-$resource-data");
});

test('compiler rejects a caller-supplied unit name or path', function () {
    app(QuadletCompiler::class)->compile(ApplicationSpec::fromArray(prebuiltApplicationSpec()), '../foreign.service');
})->throws(ValidationException::class);

test('prebuilt compiler matches reviewed golden bundle and canonical specification', function () {
    $spec = ApplicationSpec::fromArray(prebuiltApplicationSpec());
    $directory = base_path('tests/Fixtures/quadlet/prebuilt-v1');
    $canonical = trim(file_get_contents($directory.'/spec.json'));
    expect($spec->toArray())->toBe(json_decode($canonical, true, flags: JSON_THROW_ON_ERROR))
        ->and($spec->hash())->toBe(hash('sha256', $canonical));
    $name = 'coolify-11111111-1111-4111-8111-111111111111';
    expect(app(QuadletCompiler::class)->compile($spec, '11111111-1111-4111-8111-111111111111'))->toBe([
        "$name-data.volume" => file_get_contents($directory.'/data.volume'),
        "$name.container" => file_get_contents($directory.'/application.container'),
        "$name.network" => file_get_contents($directory.'/application.network'),
    ]);
});

test('unicode specification and rendered arguments share the node canonical encoding', function () {
    $directory = base_path('tests/Fixtures/quadlet/prebuilt-v1-unicode');
    $canonical = trim(file_get_contents($directory.'/spec.json'));
    $spec = ApplicationSpec::fromArray(json_decode($canonical, true, flags: JSON_THROW_ON_ERROR));
    expect($spec->hash())->toBe(hash('sha256', $canonical))
        ->and($spec->toArray()['command'][1])->toBe("line\u{2028}paragraph\u{2029}end")
        ->and($spec->toArray()['entrypoint'][1])->toBe("entry\u{2028}point\u{2029}value")
        ->and($spec->toArray()['labels'][0]['value'])->toBe("café 東京 🙂\u{2028}line\u{2029}paragraph");
    $name = 'coolify-11111111-1111-4111-8111-111111111111';
    expect(app(QuadletCompiler::class)->compile($spec, '11111111-1111-4111-8111-111111111111'))->toBe([
        "$name-data.volume" => file_get_contents($directory.'/data.volume'),
        "$name.container" => file_get_contents($directory.'/application.container'),
        "$name.network" => file_get_contents($directory.'/application.network'),
    ]);
});
