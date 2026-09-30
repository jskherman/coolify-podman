#!/usr/bin/env python3
"""Disposable-only SIGKILL after durable staging, before the journal outcome."""
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import uuid


CHILD = r'''
import hashlib, importlib.util, json, os, pwd, signal, sys
from pathlib import Path
fixture, code_hash, operation_id = sys.argv[1:]
assert sys.flags.isolated and os.getuid() == 1001 and os.getgid() == 1001
os.chdir(pwd.getpwuid(os.getuid()).pw_dir)
assert Path('/etc/coolify-disposable-fixture').read_text().strip() == fixture
source = Path('/usr/local/libexec/coolify-node-executor.py')
assert hashlib.sha256(source.read_bytes()).hexdigest() == code_hash
loader = importlib.util.spec_from_file_location('trusted_node_executor', source)
node = importlib.util.module_from_spec(loader)
loader.loader.exec_module(node)
request = json.load(sys.stdin)
assert request['operation_id'] == operation_id and request['action'] == 'stage'
directory = Path('/var/lib/coolify-node/1001')
assert directory.stat().st_uid == 1001 and directory.stat().st_mode & 0o077 == 0
os.umask(0o077)
runtime = node.SystemdRuntime(1001)
original_stage = runtime.stage
def stage_and_interrupt(request, resource):
    try:
        bundle = original_stage(request, resource)
    except Exception as exception:
        print(json.dumps({'stage_error': type(exception).__name__, 'message': str(exception)}), file=sys.stderr, flush=True)
        raise
    release = directory / 'releases' / request['resource_id'] / bundle['bundle_hash']
    files = {path.name: {'inode': path.stat().st_ino,
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                        'size': path.stat().st_size}
             for path in sorted(release.iterdir())}
    witness = {'operation_id': operation_id, 'bundle': bundle,
               'release_inode': release.stat().st_ino, 'files': files}
    with (directory / ('staging-fault-' + operation_id + '.json')).open('x') as stream:
        json.dump(witness, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    node.QuadletBundle.sync_directory(directory)
    os.kill(os.getpid(), signal.SIGKILL)
runtime.stage = stage_and_interrupt
def policy():
    value = node.trusted_policy(Path('/etc/coolify-node/policies/1001.json'))
    assert value['execution_uid'] == os.getuid() and value['execution_gid'] == os.getgid()
    return value
executor = node.Executor.open_existing(directory, runtime)
try:
    print(node.canonical(executor.execute(request, 'fixture-recovery', policy)), flush=True)
finally:
    executor.db.close()
'''


def save(path, record):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(record, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def row_for(operation_id):
    with sqlite3.connect('file:/var/lib/coolify-node/1001/journal.sqlite?mode=ro', uri=True) as database:
        database.row_factory = sqlite3.Row
        row = database.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone()
        return dict(row) if row else None


def observe(request):
    unit = 'coolify-' + request['resource_id'] + '.service'
    environment = ['runuser', '-u', 'workload', '--', 'env', 'XDG_RUNTIME_DIR=/run/user/1001',
                   'DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1001/bus']
    result = subprocess.run(environment + ['systemctl', '--user', 'show', unit,
        '--property=LoadState', '--value'], check=True, capture_output=True, text=True, timeout=10)
    invocation = subprocess.check_output(environment + ['systemctl', '--user', 'show',
        'coolify-phase0.service', '--property=InvocationID', '--value'], text=True, timeout=10).strip()
    source = Path('/home/workload/.config/containers/systemd')
    return {'load_state': result.stdout.strip(), 'phase0_invocation': invocation,
            'published_paths': sorted(str(path) for path in source.rglob('coolify-' + request['resource_id'] + '*'))}


fixture, code_hash, phase = sys.argv[1:]
assert phase in {'inject', 'inspect'}
assert os.getuid() == 0 and sys.flags.isolated
assert Path('/etc/coolify-disposable-fixture').read_text().strip() == fixture
assert Path('/etc/coolify-node/fixture-owner').read_text() == fixture
assert hashlib.sha256(Path('/usr/local/libexec/coolify-node-executor.py').read_bytes()).hexdigest() == code_hash
payload = json.load(sys.stdin)
assert set(payload) == {'base_operation_id', 'request'}
request = payload['request']
for field in ['operation_id', 'idempotency_key', 'resource_id']:
    assert str(uuid.UUID(request[field])) == request[field]
staging = json.loads(Path('/etc/coolify-node/staging-fixture.json').read_text())
assert staging['fixture'] == fixture and staging['code_hash'] == code_hash and staging['state'] == 'published'
assert request['resource_id'] == staging['resource_id']
policy = json.loads(Path('/etc/coolify-node/policies/1001.json').read_text())
assert policy == staging['new_policy'] and policy['version'] == 6 and policy['controller_epoch'] == 2
base = row_for(payload['base_operation_id'])
assert base and base['state'] == 'succeeded' and base['principal'] == 'fixture-recovery'
expected = json.loads(base['request'])
assert expected['action'] == 'stage' and expected['resource_id'] == request['resource_id']
expected['spec']['command'].append('staging-fault-' + request['operation_id'])
for field in ['operation_id', 'idempotency_key', 'deadline']:
    expected[field] = request[field]
assert request == expected
assert request['operation_id'] != base['id'] and request['idempotency_key'] != base['idempotency_key']
record_file = Path('/etc/coolify-node/staging-interruption-' + request['operation_id'] + '.json')
witness_file = Path('/var/lib/coolify-node/1001/staging-fault-' + request['operation_id'] + '.json')
with Path('/etc/coolify-node/staging-interruption.lock').open('a') as lock:
    os.fchmod(lock.fileno(), 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX)
    if record_file.exists():
        record = json.loads(record_file.read_text())
        assert record['fixture'] == fixture and record['code_hash'] == code_hash and record['payload'] == payload
    else:
        assert phase == 'inject' and row_for(request['operation_id']) is None and not witness_file.exists()
        record = {'fixture': fixture, 'code_hash': code_hash, 'payload': copy.deepcopy(payload),
                  'state': 'prepared', 'before': observe(request)}
        assert record['before']['load_state'] == 'not-found' and not record['before']['published_paths']
        save(record_file, record)
    row = row_for(request['operation_id'])
    if phase == 'inject' and row is None:
        assert record['state'] == 'prepared', 'Recorded injection has no journal intent; reconcile its process, never restart it blindly'
        record['state'] = 'injecting'
        save(record_file, record)
        process = subprocess.Popen(['runuser', '-u', 'workload', '--', '/usr/bin/python3', '-I', '-c', CHILD,
                                    fixture, code_hash, request['operation_id']],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        record['pid'] = process.pid
        record['process_start_ticks'] = Path('/proc/' + str(process.pid) + '/stat').read_text().split()[21]
        save(record_file, record)
        stdout, stderr = process.communicate(json.dumps(request), timeout=55)
        record.update(state='interrupted', process_exit=process.returncode, stdout=stdout, stderr=stderr)
        save(record_file, record)
        assert process.returncode in {-9, 137}, record
        row = row_for(request['operation_id'])
        assert row and row['state'] == 'executing' and row['outcome'] is None
    else:
        assert row is not None, 'No journal intent exists; inspect the recorded process before resuming'
    assert json.loads(row['request']) == request and row['principal'] == 'fixture-recovery'
    assert witness_file.is_file(), 'No durable witness proves the intended crash window'
    witness = json.loads(witness_file.read_text())
    assert witness['operation_id'] == request['operation_id']
    release = Path('/var/lib/coolify-node/1001/releases') / request['resource_id'] / witness['bundle']['bundle_hash']
    actual = {'release_inode': release.stat().st_ino,
              'files': {path.name: {'inode': path.stat().st_ino,
                        'sha256': hashlib.sha256(path.read_bytes()).hexdigest(), 'size': path.stat().st_size}
                        for path in sorted(release.iterdir())}}
    assert actual == {key: witness[key] for key in ['release_inode', 'files']}
    after = observe(request)
    assert after == record['before'], 'Staging changed activation state'
    print(json.dumps({'state': record['state'], 'journal_state': row['state'],
                      'journal_outcome': json.loads(row['outcome']) if row['outcome'] else None,
                      'witness': witness, 'observed': after, 'process_exit': record.get('process_exit')}))
