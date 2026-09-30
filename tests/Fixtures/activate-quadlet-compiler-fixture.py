#!/usr/bin/env python3
"""Fixed disposable compiler instrumentation, never a production deployment path."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

RESOURCE = '9c750b0e-4a61-4e6a-a465-21f59ec073a5'
PREVIOUS_RESOURCE = '9c750b0e-4a61-4e6a-a465-21f59ec073a4'
NAME = 'coolify-' + RESOURCE
IMAGE = 'docker.io/library/busybox@sha256:5cec3fc171c87218698e85a52af7087de727372aae264a787b8112901a5b0092'
ENTRYPOINT = ['sh', '-c', 'exec sleep 3600', 'compiler-fixture']
COMMAND = ['snowman ☃ café', 'double "quote"', "single 'quote'", 'back\\slash']
LABEL = 'Unicode café ☃ "quotes" back\\slash'
ENV = {'PATH': '/usr/bin:/bin', 'HOME': '/home/workload', 'LC_ALL': 'C.UTF-8',
       'XDG_RUNTIME_DIR': '/run/user/1001', 'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1001/bus'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def run(arguments, check=True):
    return subprocess.run(arguments, env=ENV, capture_output=True, text=True, timeout=40, check=check)


def properties():
    result = run(['systemctl', '--user', 'show', NAME + '.service',
                  '--property=ActiveState,InvocationID,Requires,After,MemoryMax,CPUQuotaPerSecUSec'])
    return dict(line.split('=', 1) for line in result.stdout.splitlines())


def inspect(kind, name):
    result = run(['podman', kind, 'inspect', name], check=False)
    if result.returncode:
        exists = run(['podman', kind, 'exists', name], check=False)
        assert exists.returncode == 1, result.stderr
        return None
    return json.loads(result.stdout)[0]


request = json.loads(sys.stdin.read(16385))
assert os.getuid() == 1001 and os.getgid() == 1001
assert Path('/etc/coolify-disposable-fixture').read_text().strip() == request['fixture']
assert request['resource_id'] == RESOURCE
files = request['files']
assert set(files) == {NAME + '.container', NAME + '.network', NAME + '-data.volume'}
bundle_hash = hashlib.sha256(canonical(files).encode()).hexdigest()
assert bundle_hash == request['bundle_hash']
header = '# Coolify managed resource ' + RESOURCE + '\n# Compiler prebuilt-v1.0\n# Spec '
spec_hash = request['spec_hash']
assert len(spec_hash) == 64 and all(character in '0123456789abcdef' for character in spec_hash)
header += spec_hash + '\n'
quote = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'))
expected = {
    NAME + '.network': header + f'[Network]\nNetworkName={NAME}\nInternal=true\nLabel=coolify.managed=true\nLabel=coolify.resource={RESOURCE}\n',
    NAME + '-data.volume': header + f'[Volume]\nVolumeName={NAME}-data\nLabel=coolify.managed=true\nLabel=coolify.resource={RESOURCE}\n',
    NAME + '.container': header + f'[Unit]\nDescription=Coolify application {RESOURCE}\n\n[Container]\nImage={IMAGE}\nContainerName={NAME}\nNetwork={NAME}.network\n'
        + f'PublishPort=127.0.0.1:18101:80\nLabel=coolify.managed=true\nLabel=coolify.resource={RESOURCE}\n'
        + 'Label=' + quote('fixture.value=' + LABEL) + '\nEntrypoint=' + quote(ENTRYPOINT)
        + '\nExec=' + ' '.join(quote(value) for value in COMMAND)
        + f'\nVolume={NAME}-data.volume:/data:rw\n\n[Service]\nRestart=always\nTimeoutStartSec=120\nMemoryMax=129M\nCPUQuota=125.5%\n\n[Install]\nWantedBy=default.target\n',
}
assert files == expected, 'Only the fixed compiler fixture can be activated'
unit_directory = Path('/home/workload/.config/containers/systemd')
record_directory = Path('/home/workload/.local/state') / ('compiler-fixture-' + RESOURCE)
assert not record_directory.is_symlink()
record_directory.mkdir(parents=True, mode=0o700, exist_ok=True)
record_file = record_directory / 'record.json'


def save(record):
    temporary = record_directory / 'record.tmp'
    with temporary.open('w') as stream:
        stream.write(json.dumps(record, ensure_ascii=False, indent=2))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(record_file)


def verify_owned_files():
    for filename, contents in files.items():
        path = unit_directory / filename
        if path.exists():
            assert not path.is_symlink() and path.read_text() == contents


def cleanup(record):
    verify_owned_files()
    for kind, name in [('container', NAME), ('network', NAME), ('volume', NAME + '-data')]:
        item = inspect(kind, name)
        if item:
            labels = item['Config']['Labels'] if kind == 'container' else item.get('Labels', item.get('labels', {}))
            assert labels.get('coolify.resource') == RESOURCE and labels.get('coolify.managed') == 'true'
    run(['systemctl', '--user', 'stop', NAME + '.service', NAME + '-network.service', NAME + '-data-volume.service'])
    for filename in files:
        (unit_directory / filename).unlink(missing_ok=True)
    run(['systemctl', '--user', 'daemon-reload'])
    for kind, name in [('container', NAME), ('network', NAME), ('volume', NAME + '-data')]:
        if inspect(kind, name):
            run(['podman', kind, 'rm', name])
        assert inspect(kind, name) is None
    record['state'] = 'cleaned'
    save(record)


with (record_directory / 'lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    if record_file.exists():
        record = json.loads(record_file.read_text())
        assert record['fixture'] == request['fixture'] and record['bundle_hash'] == bundle_hash
        assert record['resource_id'] == RESOURCE
        if record['state'] == 'cleaned':
            print(json.dumps(record['report'], ensure_ascii=False))
            sys.exit(0)
        if record['state'] == 'observed':
            cleanup(record)
            print(json.dumps(record['report'], ensure_ascii=False))
            sys.exit(0)
        assert record['state'] == 'activating' and properties()['ActiveState'] == 'active', 'Ambiguous prior activation; do not repeat it'
        verify_owned_files()
    else:
        previous_name = 'coolify-' + PREVIOUS_RESOURCE
        previous_record = record_directory.parent / ('compiler-fixture-' + PREVIOUS_RESOURCE) / 'record.json'
        if previous_record.exists():
            previous = json.loads(previous_record.read_text())
            assert previous['fixture'] == request['fixture'] and previous['resource_id'] == PREVIOUS_RESOURCE
            assert previous['state'] == 'cleaned', 'Prior compiler fixture must be cleaned before new verification'
            for kind, name in [('container', previous_name), ('network', previous_name), ('volume', previous_name + '-data')]:
                assert inspect(kind, name) is None
            for filename in previous['files']:
                assert not (unit_directory / filename).exists()
        assert run(['podman', 'image', 'exists', IMAGE], check=False).returncode == 0, 'Pinned image must already be cached'
        for kind, name in [('container', NAME), ('network', NAME), ('volume', NAME + '-data')]:
            assert inspect(kind, name) is None, 'Refusing adoption of existing runtime resources'
        for unit in [NAME + '.service', NAME + '-network.service', NAME + '-data-volume.service']:
            assert run(['systemctl', '--user', 'show', unit, '--property=LoadState', '--value'], check=False).stdout.strip() == 'not-found'
        for filename in files:
            assert not (unit_directory / filename).exists() and not (unit_directory / filename).is_symlink()
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 18101))
        record = {'fixture': request['fixture'], 'resource_id': RESOURCE, 'bundle_hash': bundle_hash,
                  'files': files, 'state': 'prepared'}
        save(record)
        for filename, contents in files.items():
            with (unit_directory / filename).open('x') as stream:
                stream.write(contents)
        run(['systemctl', '--user', 'daemon-reload'])
        record['state'] = 'activating'
        save(record)
        run(['systemctl', '--user', 'start', NAME + '.service'])
    observed = properties()
    container = inspect('container', NAME)
    assert observed['ActiveState'] == 'active' and observed['InvocationID']
    assert container is not None
    report = {'resource_id': RESOURCE, 'bundle_hash': bundle_hash, 'spec_hash': spec_hash,
              'entrypoint': container['Config']['Entrypoint'], 'command': container['Config']['Cmd'],
              'labels': container['Config']['Labels'], 'mounts': container['Mounts'],
              'restart_policy': container['HostConfig']['RestartPolicy']['Name'],
              'systemd': observed, 'image': container['ImageName']}
    record.update(state='observed', report=report)
    save(record)
    cleanup(record)
    print(json.dumps(report, ensure_ascii=False))
