#!/usr/bin/env python3
"""Audit a compatible executor-only repair on the recorded disposable VM."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

fixture, old_hash, new_hash = sys.argv[1:4]
expected_policy = int(sys.argv[4]) if len(sys.argv) == 5 else 9
assert expected_policy in {9, 10}
assert os.getuid() == 0
assert Path('/etc/coolify-disposable-fixture').read_text().strip() == fixture
assert Path('/etc/coolify-node/fixture-owner').read_text() == fixture
source = Path('/root/coolify-node-executor.py').read_bytes()
assert hashlib.sha256(source).hexdigest() == new_hash
installed = Path('/usr/local/libexec/coolify-node-executor.py')
record_file = Path('/etc/coolify-node/executor-repair-' + new_hash + '.json')

def save(path, contents, mode):
    temporary = path.with_suffix('.tmp')
    with temporary.open('wb') as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

with open('/var/lib/coolify-node/1001/executor.lock', 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    observed_hash = hashlib.sha256(installed.read_bytes()).hexdigest()
    assert observed_hash in [old_hash, new_hash]
    policy = json.loads(Path('/etc/coolify-node/policies/1001.json').read_text())
    assert policy['version'] == expected_policy and policy['controller_epoch'] == 2
    if record_file.exists():
        record = json.loads(record_file.read_text())
        assert record['fixture'] == fixture and record['old_hash'] == old_hash and record['new_hash'] == new_hash
    else:
        assert observed_hash == old_hash
        with sqlite3.connect('file:/var/lib/coolify-node/1001/journal.sqlite?mode=ro', uri=True) as db:
            record = {'fixture': fixture, 'state': 'prepared', 'old_hash': old_hash, 'new_hash': new_hash,
                      'policy_version': expected_policy, 'generations': db.execute('select id,generation from resources').fetchall(),
                      'pending': db.execute("select id,state,before_state,payload_hash from operations where state in ('executing','needs_intervention')").fetchall()}
        save(Path('/etc/coolify-node/executor-before-' + new_hash + '.py'), installed.read_bytes(), 0o600)
        save(record_file, json.dumps(record).encode(), 0o600)
    save(installed, source, 0o755)
    record['state'] = 'published'
    save(record_file, json.dumps(record).encode(), 0o600)
print(json.dumps({'state': record['state'], 'executor_hash': new_hash, 'pending': record['pending']}))
