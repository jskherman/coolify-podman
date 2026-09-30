#!/usr/bin/env python3
"""One audited compatible adapter repair; never a production upgrade mechanism."""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

fixture, executor_hash, adapter_hash, operation_id = sys.argv[1:]
assert os.getuid() == 0
assert Path('/etc/coolify-disposable-fixture').read_text().strip() == fixture
assert Path('/etc/coolify-node/fixture-owner').read_text() == fixture
sources = {name: Path('/root/' + name).read_bytes() for name in ['coolify-node-executor.py', 'coolify-node-native.py']}
assert hashlib.sha256(sources['coolify-node-executor.py']).hexdigest() == executor_hash
assert hashlib.sha256(sources['coolify-node-native.py']).hexdigest() == adapter_hash
policy_file = Path('/etc/coolify-node/policies/1001.json')
record_file = Path('/etc/coolify-node/native-generator-upgrade.json')

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))

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
    policy = json.loads(policy_file.read_text())
    with sqlite3.connect('/var/lib/coolify-node/1001/journal.sqlite') as db:
        db.row_factory = sqlite3.Row
        row = db.execute('select * from operations where id=?', (operation_id,)).fetchone()
        if record_file.exists():
            record = json.loads(record_file.read_text())
            assert record['fixture'] == fixture and record['executor_hash'] == executor_hash and record['adapter_hash'] == adapter_hash
            assert record['operation_id'] == operation_id and policy in [record['old_policy'], record['new_policy']]
        else:
            assert policy['version'] == 7 and policy['controller_epoch'] == 2
            assert row['state'] == 'needs_intervention'
            before = json.loads(row['before_state']);request=json.loads(row['request']);resource=request['resource_id']
            assert before['phase'] == 'publishing' and before['previous_bundle_hash'] is None
            grant = policy['resources'][resource]
            old_definition = hashlib.sha256(canonical({k:v for k,v in grant.items() if k != 'generation'}).encode()).hexdigest()
            assert before['definition_hash'] == old_definition
            unit = 'coolify-' + resource + '.service'
            state = subprocess.check_output(['/usr/sbin/runuser','-u','workload','--','env','XDG_RUNTIME_DIR=/run/user/1001',
                                             'systemctl','--user','show',unit,'--property=ActiveState','--value'],text=True).strip()
            assert state == 'inactive'
            container = subprocess.run(['/usr/sbin/runuser','-u','workload','--','env','HOME=/home/workload','XDG_RUNTIME_DIR=/run/user/1001',
                                        'podman','--remote=false','container','exists','coolify-' + resource],cwd='/home/workload')
            assert container.returncode == 1
            selected = Path('/home/workload/.config/containers/systemd/coolify-' + resource)
            assert selected.is_symlink() and os.readlink(selected) == '/var/lib/coolify-node/1001/releases/' + resource + '/' + request['bundle_hash']
            new = json.loads(json.dumps(policy));new['version'] = 8
            new['resources'][resource]['native_adapter_sha256'] = adapter_hash
            new_before = {**before, 'definition_hash': hashlib.sha256(canonical({k:v for k,v in grant.items() if k not in ['generation','native_adapter_sha256']}).encode()).hexdigest()}
            record = {'fixture': fixture, 'operation_id': operation_id, 'state': 'prepared', 'old_policy': policy, 'new_policy': new,
                      'old_before': before, 'new_before': new_before, 'request_hash': row['payload_hash'],
                      'executor_hash': executor_hash, 'adapter_hash': adapter_hash,
                      'reason': 'Validate with identical pinned user-generator; separate adapter code identity from desired resource identity.'}
            save(record_file, json.dumps(record).encode(), 0o600)
        assert row['payload_hash'] == record['request_hash']
        assert json.loads(row['before_state']) in [record['old_before'],record['new_before']]
        for name, source in sources.items():
            save(Path('/usr/local/libexec/' + name),source,0o755)
        db.execute('update operations set before_state=? where id=?',(canonical(record['new_before']),operation_id))
        db.commit()
        save(policy_file,json.dumps(record['new_policy']).encode(),0o644)
        record['state']='published'
        save(record_file,json.dumps(record).encode(),0o600)
print(json.dumps({'state':record['state'],'policy_version':8,'recovery_target':operation_id}))
