#!/usr/bin/env python3
"""Require UID/GID-bound protocol on the marker-checked disposable node."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import sys

fixture, code_hash = sys.argv[1:]
assert os.getuid() == 0
assert Path('/etc/coolify-disposable-fixture').read_text().strip() == fixture
assert Path('/etc/coolify-node/fixture-owner').read_text() == fixture
identity = pwd.getpwnam('workload')
assert identity.pw_uid == 1001 and identity.pw_gid == 1001
source = Path('/root/coolify-node-executor.py').read_bytes()
assert hashlib.sha256(source).hexdigest() == code_hash
policy_file = Path('/etc/coolify-node/policies/1001.json')
record_file = Path('/etc/coolify-node/identity-fixture.json')
with open('/var/lib/coolify-node/1001/executor.lock', 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    current = json.loads(policy_file.read_text())
    if record_file.exists():
        record = json.loads(record_file.read_text())
        assert record['fixture'] == fixture and record['code_hash'] == code_hash
        assert current in [record['old_policy'], record['new_policy']]
    else:
        assert current['version'] == 4 and current['controller_epoch'] == 2
        new_policy = json.loads(json.dumps(current))
        new_policy.update(version=5, minimum_protocol=2, execution_gid=identity.pw_gid)
        record = {'fixture': fixture, 'code_hash': code_hash, 'state': 'prepared',
                  'old_policy': current, 'new_policy': new_policy}
        record_file.write_text(json.dumps(record))
    destination = Path('/usr/local/libexec/coolify-node-executor.py')
    candidate = destination.with_suffix('.candidate')
    candidate.write_bytes(source)
    candidate.chmod(0o755)
    candidate.replace(destination)
    candidate = policy_file.with_suffix('.tmp')
    candidate.write_text(json.dumps(record['new_policy']))
    candidate.chmod(0o644)
    candidate.replace(policy_file)
    record['state'] = 'published'
    temporary = record_file.with_suffix('.tmp')
    temporary.write_text(json.dumps(record))
    temporary.replace(record_file)
print(json.dumps({'state': record['state'], 'policy_version': 5, 'minimum_protocol': 2, 'code_hash': code_hash}))
