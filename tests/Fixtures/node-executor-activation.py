#!/usr/bin/env python3
"""Recorded policy7/code transition on the expressly disposable identity fixture."""
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
record_file = Path('/etc/coolify-node/activation-fixture.json')

def save(path, content, mode):
    temporary = path.with_suffix('.tmp')
    with temporary.open('wb') as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(content)
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
        assert record['resource_id'] == resource_id
        assert current in [record['old_policy'], record['new_policy']]
    else:
        assert current['version'] == 6 and current['controller_epoch'] == 2 and current['minimum_protocol'] == 2
        with sqlite3.connect('file:/var/lib/coolify-node/1001/journal.sqlite?mode=ro', uri=True) as db:
            assert not db.execute("select id from operations where state in ('executing','needs_intervention')").fetchall()
            assert db.execute('select generation from resources where id=?', (resource_id,)).fetchone()[0] == 0
        new = json.loads(json.dumps(current))
        new['version'] = 7
        assert new['resources'][resource_id]['kind'] == 'quadlet'
        new['resources'][resource_id].update(native_adapter_sha256=adapter_hash, activation_timeout_seconds=10)
        new['principals']['fixture-recovery']['actions'].append('activate')
        record = {'fixture': fixture, 'executor_hash': executor_hash, 'adapter_hash': adapter_hash,
                  'resource_id': resource_id, 'state': 'prepared', 'old_policy': current, 'new_policy': new}
        save(record_file, json.dumps(record).encode(), 0o600)
    ancestor = Path('/home/workload/.config/containers')
    identity = ancestor.lstat()
    assert identity.st_mode & 0o022 == 0 and ancestor.is_dir() and not ancestor.is_symlink()
    if 'ownership_repair' not in record:
        assert identity.st_uid in [0, 1001] and identity.st_gid in [0, 1001]
        record['ownership_repair'] = {'operation_id': fixture + '-native-config-owner', 'state': 'prepared',
                                      'path': str(ancestor), 'inode': identity.st_ino,
                                      'previous_uid': identity.st_uid, 'previous_gid': identity.st_gid}
        save(record_file, json.dumps(record).encode(), 0o600)
    repair = record['ownership_repair']
    assert identity.st_ino == repair['inode'] and identity.st_uid in [repair['previous_uid'], 1001]
    if identity.st_uid != 1001 or identity.st_gid != 1001:
        os.chown(ancestor, 1001, 1001)
    assert ancestor.stat().st_uid == ancestor.stat().st_gid == 1001
    repair['state'] = 'succeeded'
    save(record_file, json.dumps(record).encode(), 0o600)
    for name, source in sources.items():
        save(Path('/usr/local/libexec/' + name), source, 0o755)
    save(policy_file, json.dumps(record['new_policy']).encode(), 0o644)
    record['state'] = 'published'
    save(record_file, json.dumps(record).encode(), 0o600)
print(json.dumps({'state': record['state'], 'policy_version': 7, 'resource_id': resource_id}))
