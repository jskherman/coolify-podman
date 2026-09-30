#!/usr/bin/env python3
"""Disposable-fixture bootstrap only. Never an operational-agent tool."""
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import subprocess
import sys
import uuid

fixture, resource = sys.argv[1:]
assert os.getuid() == 0
assert Path('/etc/coolify-disposable-fixture').read_text().strip() == fixture
assert str(uuid.UUID(resource)) == resource
owner = Path('/etc/coolify-node/fixture-owner')
if owner.exists():
    assert owner.read_text() == fixture
else:
    assert not owner.parent.exists(), 'Refusing adoption of existing node configuration'
    owner.parent.mkdir(mode=0o755)
    owner.write_text(fixture)
identity = pwd.getpwnam('workload')
uid = identity.pw_uid
policy_dir = owner.parent / 'policies'
policy_dir.mkdir(mode=0o755, exist_ok=True)
state = Path(f'/var/lib/coolify-node/{uid}')
state.mkdir(mode=0o700, parents=True, exist_ok=True)
os.chown(state, uid, identity.pw_gid)
os.chmod(state, 0o700)
program = Path('/usr/local/libexec/coolify-node-executor.py')
program.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
candidate = program.with_suffix('.candidate')
shutil.copyfile('/root/coolify-node-executor.py', candidate)
os.chmod(candidate, 0o755)
with candidate.open('rb') as stream:
    os.fsync(stream.fileno())
candidate.replace(program)
subprocess.run(['runuser', '-u', 'workload', '--', '/usr/bin/python3', '-I', '-c',
                'import runpy; from pathlib import Path; '
                f'namespace=runpy.run_path({str(program)!r}, run_name="fixture_bootstrap"); '
                f'namespace["Executor"](Path({str(state)!r}), None)'], check=True, cwd='/home/workload')
source = Path('/home/workload/.config/containers/systemd/coolify-phase0.container')
assert f'# {fixture}' in source.read_text().splitlines()
effective = subprocess.check_output([
    'runuser', '-u', 'workload', '--', 'env', f'XDG_RUNTIME_DIR=/run/user/{uid}',
    f'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/{uid}/bus',
    'systemctl', '--user', 'cat', 'coolify-phase0.service',
], cwd='/home/workload')
policy = {
    'execution_uid': uid, 'execution_gid': identity.pw_gid, 'minimum_protocol': 2, 'version': 1, 'controller_epoch': 1,
    'principals': {'fixture-controller': {'resources': [resource], 'actions': ['status', 'start', 'stop', 'restart']}},
    'resources': {resource: {'unit': 'coolify-phase0.service', 'generation': 1,
                           'source': str(source), 'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                           'effective_sha256': hashlib.sha256(effective).hexdigest()}},
}
policy_file = policy_dir / f'{uid}.json'
if policy_file.exists():
    assert json.loads(policy_file.read_text()) == policy, 'Refusing to reset changed authority'
else:
    policy_file.write_text(json.dumps(policy))
    os.chmod(policy_file, 0o644)
key = Path('/root/node_ed25519.pub').read_text().strip()
assert key.startswith('ssh-ed25519 ') and '\n' not in key
line = f'restrict,command="/usr/bin/python3 -I {program} --principal fixture-controller" {key}'
authorized = Path('/home/workload/.ssh/authorized_keys')
existing = authorized.read_text()
if line not in existing.splitlines():
    assert key.split()[1] not in existing, 'Same key already has different authority'
    authorized.write_text(existing.rstrip() + '\n' + line + '\n')
print(json.dumps({'fixture': fixture, 'resource': resource, 'execution_uid': uid, 'state': 'provisioned'}))
