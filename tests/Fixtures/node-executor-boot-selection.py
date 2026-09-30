#!/usr/bin/env python3
"""Publish the audited boot-selection executor/adapter on a disposable node."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

fixture, executor_hash, adapter_hash, resource_id = sys.argv[1:]
assert os.getuid() == 0
assert Path('/etc/coolify-disposable-fixture').read_text().strip() == fixture
assert Path('/etc/coolify-node/fixture-owner').read_text() == fixture
sources = {name: Path('/root/' + name).read_bytes() for name in ['coolify-node-executor.py', 'coolify-node-native.py']}
assert hashlib.sha256(sources['coolify-node-executor.py']).hexdigest() == executor_hash
assert hashlib.sha256(sources['coolify-node-native.py']).hexdigest() == adapter_hash
policy_file = Path('/etc/coolify-node/policies/1001.json')
record_file = Path('/etc/coolify-node/boot-selection-fixture.json')

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
    current = json.loads(policy_file.read_text())
    if record_file.exists():
        record = json.loads(record_file.read_text())
        assert record['fixture'] == fixture and record['executor_hash'] == executor_hash and record['adapter_hash'] == adapter_hash
        assert current in [record['old_policy'], record['new_policy']]
    else:
        assert current['version'] in {7, 9} and current['controller_epoch'] == 2
        if current['version'] == 7:
            assert hashlib.sha256(Path('/usr/local/libexec/coolify-node-executor.py').read_bytes()).hexdigest() == executor_hash
            assert hashlib.sha256(Path('/usr/local/libexec/coolify-node-native.py').read_bytes()).hexdigest() == adapter_hash
        with sqlite3.connect('file:/var/lib/coolify-node/1001/journal.sqlite?mode=ro', uri=True) as db:
            assert not db.execute("select id from operations where state in ('executing','needs_intervention')").fetchall()
            assert db.execute('select generation from resources where id=?', (resource_id,)).fetchone()[0] == 2
        new = json.loads(json.dumps(current))
        new['version'] = 10
        new['resources'][resource_id]['native_adapter_sha256'] = adapter_hash
        record = {'fixture': fixture, 'state': 'prepared', 'executor_hash': executor_hash,
                  'adapter_hash': adapter_hash, 'old_policy': current, 'new_policy': new, 'resource_id': resource_id}
        save(record_file, json.dumps(record).encode(), 0o600)
    for name, contents in sources.items():
        save(Path('/usr/local/libexec/' + name), contents, 0o755)
    save(policy_file, json.dumps(record['new_policy']).encode(), 0o644)
    record['state'] = 'published'
    save(record_file, json.dumps(record).encode(), 0o600)
print(json.dumps({'state': record['state'], 'policy_version': 10, 'resource_id': resource_id}))
