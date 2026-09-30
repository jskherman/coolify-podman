#!/usr/bin/env python3
"""Publish an audited compatible executor/adapter pair on a disposable node."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys

fixture, old_executor, old_adapter, new_executor, new_adapter, resource_id, generation = sys.argv[1:]
generation = int(generation)
assert os.getuid() == 0
assert Path('/etc/coolify-disposable-fixture').read_text().strip() == fixture
assert Path('/etc/coolify-node/fixture-owner').read_text() == fixture
names = ['coolify-node-executor.py', 'coolify-node-native.py']
sources = {name: Path('/root/' + name).read_bytes() for name in names}
assert [hashlib.sha256(sources[name]).hexdigest() for name in names] == [new_executor, new_adapter]
policy_file = Path('/etc/coolify-node/policies/1001.json')
record_file = Path('/etc/coolify-node/release-continuation-' + new_executor + '.json')

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
    policy = json.loads(policy_file.read_text())
    installed = [hashlib.sha256(Path('/usr/local/libexec/' + name).read_bytes()).hexdigest() for name in names]
    assert policy['version'] == 10 and policy['controller_epoch'] == 2
    if record_file.exists():
        record = json.loads(record_file.read_text())
        assert record['fixture'] == fixture and record['resource_id'] == resource_id
        assert record['old_hashes'] == [old_executor, old_adapter] and record['new_hashes'] == [new_executor, new_adapter]
        assert policy in [record['old_policy'], record['new_policy']]
        assert all(value in {old, new} for value, old, new in zip(installed, record['old_hashes'], record['new_hashes']))
    else:
        assert installed == [old_executor, old_adapter]
        assert policy['resources'][resource_id]['native_adapter_sha256'] == old_adapter
        new_policy = json.loads(json.dumps(policy))
        new_policy['resources'][resource_id]['native_adapter_sha256'] = new_adapter
        with sqlite3.connect('file:/var/lib/coolify-node/1001/journal.sqlite?mode=ro', uri=True) as db:
            assert not db.execute("select id from operations where state in ('executing','needs_intervention')").fetchall()
            assert db.execute('select generation from resources where id=?', (resource_id,)).fetchone()[0] == generation
            record = {'fixture': fixture, 'resource_id': resource_id, 'state': 'prepared',
                      'old_hashes': installed, 'new_hashes': [new_executor, new_adapter],
                      'old_policy': policy, 'new_policy': new_policy,
                      'generations': db.execute('select id,generation from resources').fetchall()}
        for name in names:
            save(Path('/etc/coolify-node/before-' + new_executor + '-' + name), Path('/usr/local/libexec/' + name).read_bytes(), 0o600)
        save(record_file, json.dumps(record).encode(), 0o600)
    if record['state'] != 'published':
        for name in names:
            save(Path('/usr/local/libexec/' + name), sources[name], 0o755)
        save(policy_file, json.dumps(record['new_policy']).encode(), 0o644)
        record['state'] = 'published'
        save(record_file, json.dumps(record).encode(), 0o600)
    else:
        assert installed == [new_executor, new_adapter] and policy == record['new_policy']
print(json.dumps({'state': record['state'], 'hashes': record['new_hashes'], 'generations': record['generations']}))
