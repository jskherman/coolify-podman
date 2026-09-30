#!/usr/bin/env python3
"""Grant one native resource to the restricted executor on the disposable fixture."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import uuid

fixture, code_hash, resource_id = sys.argv[1:]
assert os.getuid() == 0
assert str(uuid.UUID(resource_id)) == resource_id
assert Path('/etc/coolify-disposable-fixture').read_text().strip() == fixture
assert Path('/etc/coolify-node/fixture-owner').read_text() == fixture
source = Path('/root/coolify-node-executor.py').read_bytes()
assert hashlib.sha256(source).hexdigest() == code_hash
policy_file = Path('/etc/coolify-node/policies/1001.json')
record_file = Path('/etc/coolify-node/staging-fixture.json')

def save(path, contents, mode):
    temporary = path.with_suffix('.tmp')
    with temporary.open('wb') as stream:
        os.fchmod(stream.fileno(), mode)
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
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
        assert record['fixture'] == fixture and record['code_hash'] == code_hash and record['resource_id'] == resource_id
        assert current in [record['old_policy'], record['new_policy']]
    else:
        assert current['version'] == 5 and current['controller_epoch'] == 2 and current['minimum_protocol'] == 2
        with sqlite3.connect('file:/var/lib/coolify-node/1001/journal.sqlite?mode=ro', uri=True) as db:
            assert db.execute("SELECT count(*) FROM operations WHERE state IN ('executing','needs_intervention')").fetchone()[0] == 0
        generator = Path('/usr/lib/systemd/system-generators/podman-system-generator')
        new_policy = json.loads(json.dumps(current))
        new_policy['version'] = 6
        new_policy['resources'][resource_id] = {
            'kind': 'quadlet', 'profile': 'prebuilt-v1', 'generation': 0,
            'host_ports': [18100], 'image_registries': ['docker.io'],
            'max_memory_mib': 256, 'max_cpu_millis': 1000, 'max_volumes': 2,
            'podman_version': '5.4.2', 'generator_sha256': hashlib.sha256(generator.read_bytes()).hexdigest(),
        }
        new_policy['principals']['fixture-recovery']['resources'].append(resource_id)
        new_policy['principals']['fixture-recovery']['actions'].append('stage')
        record = {'fixture': fixture, 'code_hash': code_hash, 'resource_id': resource_id,
                  'state': 'prepared', 'old_policy': current, 'new_policy': new_policy}
        save(record_file, json.dumps(record).encode(), 0o600)
    save(Path('/usr/local/libexec/coolify-node-executor.py'), source, 0o755)
    save(policy_file, json.dumps(record['new_policy']).encode(), 0o644)
    record['state'] = 'published'
    save(record_file, json.dumps(record).encode(), 0o600)
print(json.dumps({'state': record['state'], 'resource_id': resource_id, 'policy_version': 6, 'code_hash': code_hash}))
