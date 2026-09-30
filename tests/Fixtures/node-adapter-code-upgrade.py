#!/usr/bin/env python3
"""Audited compatible adapter-only repair on the disposable boot fixture."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

fixture, old_hash, new_hash, resource_id = sys.argv[1:]
assert os.getuid() == 0
assert Path('/etc/coolify-disposable-fixture').read_text().strip() == fixture
assert Path('/etc/coolify-node/fixture-owner').read_text() == fixture
source = Path('/root/coolify-node-native.py').read_bytes()
assert hashlib.sha256(source).hexdigest() == new_hash
installed = Path('/usr/local/libexec/coolify-node-native.py')
record_file = Path('/etc/coolify-node/adapter-repair-' + new_hash + '.json')
policy_file = Path('/etc/coolify-node/policies/1001.json')

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
    assert observed_hash in {old_hash, new_hash}
    policy = json.loads(policy_file.read_text())
    assert policy['version'] == 10 and policy['controller_epoch'] == 2
    if record_file.exists():
        record = json.loads(record_file.read_text())
        assert record['fixture'] == fixture and record['old_hash'] == old_hash and record['new_hash'] == new_hash
        assert policy in [record['old_policy'], record['new_policy']]
    else:
        assert observed_hash == old_hash and policy['resources'][resource_id]['native_adapter_sha256'] == old_hash
        new_policy = json.loads(json.dumps(policy))
        new_policy['resources'][resource_id]['native_adapter_sha256'] = new_hash
        with sqlite3.connect('file:/var/lib/coolify-node/1001/journal.sqlite?mode=ro', uri=True) as db:
            record = {'fixture': fixture, 'state': 'prepared', 'old_hash': old_hash, 'new_hash': new_hash,
                      'old_policy': policy, 'new_policy': new_policy,
                      'generations': db.execute('select id,generation from resources').fetchall(),
                      'pending': db.execute("select id,state,before_state,payload_hash,request from operations where state in ('executing','needs_intervention')").fetchall()}
        save(Path('/etc/coolify-node/adapter-before-' + new_hash + '.py'), installed.read_bytes(), 0o600)
        save(record_file, json.dumps(record).encode(), 0o600)
    save(installed, source, 0o755)
    save(policy_file, json.dumps(record['new_policy']).encode(), 0o644)
    record['state'] = 'published'
    save(record_file, json.dumps(record).encode(), 0o600)
print(json.dumps({'state': record['state'], 'adapter_hash': new_hash, 'pending': record['pending']}))
