#!/usr/bin/env python3
"""Marker-checked authority handoff and process fault injection, disposable VM only."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import subprocess
import sys
import time
import uuid

fixture, phase, operation_id, code_hash = sys.argv[1:]
assert os.getuid() == 0
assert Path('/etc/coolify-disposable-fixture').read_text().strip() == fixture
assert Path('/etc/coolify-node/fixture-owner').read_text() == fixture
assert str(uuid.UUID(operation_id)) == operation_id
record_file = Path('/etc/coolify-node/handoff-fixture.json')
policy_file = Path('/etc/coolify-node/policies/1001.json')


def save(record):
    temporary = record_file.with_suffix('.tmp')
    temporary.write_text(json.dumps(record))
    temporary.replace(record_file)


def active():
    return subprocess.check_output(['runuser', '-u', 'workload', '--', 'env', 'XDG_RUNTIME_DIR=/run/user/1001',
        'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus', 'systemctl', '--user', 'show',
        'coolify-phase0.service', '--property=ActiveState', '--value'], cwd='/home/workload').decode().strip()


if phase == 'prepare':
    if record_file.exists():
        record = json.loads(record_file.read_text())
        assert record['operation_id'] == operation_id and record['code_hash'] == code_hash
        print(json.dumps(record))
        sys.exit(0)
    with open('/var/lib/coolify-node/1001/executor.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        policy = json.loads(policy_file.read_text())
        assert policy['version'] == 2 and policy['controller_epoch'] == 1
        source = Path('/root/coolify-node-executor.py').read_bytes()
        assert hashlib.sha256(source).hexdigest() == code_hash
        destination = Path('/usr/local/libexec/coolify-node-executor.py')
        candidate = destination.with_suffix('.candidate')
        candidate.write_bytes(source)
        candidate.chmod(0o755)
        candidate.replace(destination)
        public_key = Path('/root/node_recovery_ed25519.pub').read_text().strip().split()
        assert public_key[0] == 'ssh-ed25519' and re.fullmatch(r'[A-Za-z0-9+/=]+', public_key[1])
        line = 'restrict,command="/usr/bin/python3 -I /usr/local/libexec/coolify-node-executor.py --principal fixture-recovery" ' + ' '.join(public_key[:2])
        authorized = Path('/home/workload/.ssh/authorized_keys')
        lines = authorized.read_text().splitlines()
        if line not in lines:
            authorized.write_text('\n'.join(lines + [line]) + '\n')
        save({'operation_id': operation_id, 'code_hash': code_hash, 'state': 'prepared', 'old_policy': policy})
elif phase == 'interrupt':
    record = json.loads(record_file.read_text())
    assert record['operation_id'] == operation_id
    if record['state'] == 'prepared':
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            database = sqlite3.connect('file:/var/lib/coolify-node/1001/journal.sqlite?mode=ro', uri=True)
            try:
                row = database.execute('SELECT state FROM operations WHERE id=?', (operation_id,)).fetchone()
            finally:
                database.close()
            if row and row[0] == 'executing' and active() == 'activating':
                pids = subprocess.check_output(['pgrep', '-f', '^/usr/bin/python3 -I /usr/local/libexec/coolify-node-executor.py --principal fixture-controller$']).split()
                assert len(pids) == 1
                os.kill(int(pids[0]), signal.SIGKILL)
                record['state'] = 'interrupted'
                save(record)
                break
            time.sleep(0.2)
        else:
            raise RuntimeError('No safe fault window; inspect journal, never repeat the restart')
    assert record['state'] in ['interrupted', 'transferred']
elif phase == 'transfer':
    record = json.loads(record_file.read_text())
    assert record['operation_id'] == operation_id and record['state'] in ['interrupted', 'transferred']
    with open('/var/lib/coolify-node/1001/executor.lock', 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        policy = dict(record['old_policy'])
        policy.update(version=3, controller_epoch=2,
                      principals={'fixture-recovery': {'resources': list(policy['resources']),
                                  'actions': ['status', 'recover', 'start', 'stop', 'restart']}})
        actual = json.loads(policy_file.read_text())
        assert actual in [record['old_policy'], policy]
        candidate = policy_file.with_suffix('.tmp')
        candidate.write_text(json.dumps(policy))
        candidate.chmod(0o644)
        candidate.replace(policy_file)
        record['state'] = 'transferred'
        save(record)
elif phase == 'ready':
    deadline = time.monotonic() + 60
    while active() != 'active':
        assert time.monotonic() < deadline, 'Activation has not converged; preserve pending recovery'
        time.sleep(0.5)
else:
    raise ValueError('Unknown phase')
print(json.dumps(json.loads(record_file.read_text())))
