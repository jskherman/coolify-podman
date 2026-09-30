#!/usr/bin/env python3
"""Root-only fault injection for the marker-verified disposable fixture."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

fixture, phase, operation_id = sys.argv[1:]
assert os.getuid() == 0
assert Path('/etc/coolify-disposable-fixture').read_text().strip() == fixture
assert Path('/etc/coolify-node/fixture-owner').read_text() == fixture
assert str(uuid.UUID(operation_id)) == operation_id
policy_file = Path('/etc/coolify-node/policies/1001.json')
record_file = Path('/etc/coolify-node/interruption-fixture.json')
environment = ['runuser', '-u', 'workload', '--', 'env', 'XDG_RUNTIME_DIR=/run/user/1001',
               'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus']


def systemctl(*args):
    return subprocess.check_output(environment + ['systemctl', '--user', *args], cwd='/home/workload')


def save(record):
    temporary = record_file.with_suffix('.tmp')
    temporary.write_text(json.dumps(record))
    temporary.replace(record_file)


if phase == 'prepare':
    assert not record_file.exists(), 'Reconcile existing fault injection before retrying'
    policy = json.loads(policy_file.read_text())
    assert policy['version'] == 1
    assert len(policy['resources']) == 1
    resource = next(iter(policy['resources'].values()))
    source = Path(resource['source'])
    assert hashlib.sha256(source.read_bytes()).hexdigest() == resource['source_sha256']
    with open('/var/lib/coolify-node/1001/executor.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        save({'fixture': fixture, 'operation_id': operation_id, 'state': 'preparing'})
        previous = source.with_suffix('.interruption-previous')
        assert not previous.exists()
        previous.write_bytes(source.read_bytes())
        source.write_text(source.read_text() + '\n[Service]\nExecStartPre=/usr/bin/sleep 12\n')
        systemctl('daemon-reload')
        resource['source_sha256'] = hashlib.sha256(source.read_bytes()).hexdigest()
        resource['effective_sha256'] = hashlib.sha256(systemctl('cat', resource['unit'])).hexdigest()
        policy['version'] = 2
        candidate = policy_file.with_suffix('.tmp')
        candidate.write_text(json.dumps(policy))
        os.chmod(candidate, 0o644)
        candidate.replace(policy_file)
        save({'fixture': fixture, 'operation_id': operation_id, 'state': 'prepared', 'policy_version': 2})
    print('prepared')
elif phase == 'interrupt':
    record = json.loads(record_file.read_text())
    assert record['operation_id'] == operation_id and record['state'] == 'prepared'
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        with sqlite3.connect('file:/var/lib/coolify-node/1001/journal.sqlite?mode=ro', uri=True) as database:
            row = database.execute('SELECT state FROM operations WHERE id=?', (operation_id,)).fetchone()
        active = systemctl('show', 'coolify-phase0.service', '--property=ActiveState', '--value').decode().strip()
        if row and row[0] == 'executing' and active == 'activating':
            pids = subprocess.check_output(['pgrep', '-f', '^/usr/bin/python3 -I /usr/local/libexec/coolify-node-executor.py --principal fixture-controller$']).split()
            assert len(pids) == 1, 'Refusing ambiguous process identity'
            os.kill(int(pids[0]), signal.SIGKILL)
            record['state'] = 'killed_after_systemd_activation_started'
            save(record)
            print(record['state'])
            break
        time.sleep(0.2)
    else:
        raise RuntimeError('Fault window not observed; do not replay a new restart')
else:
    raise ValueError('Unknown fixture phase')
