#!/usr/bin/env python3
"""Disposable SIGKILL at a named actual release effect or publication boundary."""
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
fixture, code_hash, fault_id, target = sys.argv[1:]
assert sys.flags.isolated and os.getuid() == 1001 and os.getgid() == 1001
assert Path('/etc/coolify-disposable-fixture').read_text().strip() == fixture
os.chdir(pwd.getpwuid(1001).pw_dir)
os.umask(0o077)
source = Path('/usr/local/libexec/coolify-node-executor.py')
assert hashlib.sha256(source.read_bytes()).hexdigest() == code_hash
loader = importlib.util.spec_from_file_location('trusted_executor', source)
node = importlib.util.module_from_spec(loader)
loader.loader.exec_module(node)
request = json.load(sys.stdin)
runtime = node.SystemdRuntime(1001)
native = runtime.native
executor = node.Executor.open_existing(Path('/var/lib/coolify-node/1001'), runtime)
def phase():
    row = executor.db.execute('select before_state from operations where id=?', (request['operation_id'],)).fetchone()
    return json.loads(row[0])
def cut():
    record = {'fault_id': fault_id, 'operation_id': request['operation_id'], 'target': target,
              'before_state': phase(), 'boot_id': native.boot_id(),
              'selection': native.current(request['resource_id'], policy()['resources'][request['resource_id']]),
              'persistent': native.selection(request['resource_id']).is_symlink(),
              'volatile': native.volatile_selection(request['resource_id']).is_symlink()}
    if target.startswith('lifecycle_'):
        record['owner_commands'] = native.run(['/usr/bin/systemctl', '--user', 'show',
            native.resource_name(request['resource_id']) + '.service',
            '--property=LoadState,ExecStop,ExecStopPost']).stdout
    path = Path('/var/lib/coolify-node/1001/release-cutpoint-' + fault_id + '.json')
    with path.open('x') as stream:
        json.dump(record, stream)
        stream.flush()
        os.fsync(stream.fileno())
    node.QuadletBundle.sync_directory(path.parent)
    os.kill(os.getpid(), signal.SIGKILL)
original_start = native.start
def start(resource_id, grant, before_request=None):
    original_start(resource_id, grant, before_request)
    if phase()['phase'] == target:
        cut()
native.start = start
original_commit = native.commit
def commit(resource_id, bundle_hash, operation_id, grant, restore_stopped=False):
    if bundle_hash == request['bundle_hash'] and target == 'accepted_before_publication':
        assert phase()['phase'] == 'committed'
        cut()
    result = original_commit(resource_id, bundle_hash, operation_id, grant, restore_stopped=restore_stopped)
    if bundle_hash == request['bundle_hash'] and target == 'accepted_after_publication':
        assert phase()['phase'] == 'committed'
        cut()
    return result
native.commit = commit
original_prepare = native.prepare_lifecycle
def prepare(*args):
    original_prepare(*args)
    if target == 'lifecycle_prepared':
        cut()
native.prepare_lifecycle = prepare
original_request = native.request_lifecycle
def lifecycle_request(*args):
    original_request(*args)
    if target == 'lifecycle_requested':
        cut()
native.request_lifecycle = lifecycle_request
policy = lambda: node.trusted_policy(Path('/etc/coolify-node/policies/1001.json'))
try:
    print(node.canonical(executor.execute(request, 'fixture-recovery', policy)), flush=True)
finally:
    executor.db.close()
'''

fixture, code_hash, fault_id, target = sys.argv[1:]
assert os.getuid() == 0 and sys.flags.isolated
assert Path('/etc/coolify-disposable-fixture').read_text().strip() == fixture
assert Path('/etc/coolify-node/fixture-owner').read_text() == fixture
assert str(uuid.UUID(fault_id)) == fault_id
assert target in {'accepted_before_publication', 'accepted_after_publication', 'accepted_restart', 'compensating_start',
                  'lifecycle_prepared', 'lifecycle_requested'}
assert hashlib.sha256(Path('/usr/local/libexec/coolify-node-executor.py').read_bytes()).hexdigest() == code_hash
request = json.load(sys.stdin)
for field in ['operation_id', 'resource_id', 'idempotency_key']:
    assert str(uuid.UUID(request[field])) == request[field]
assert request['policy_version'] == 10
assert request['action'] in ({'start', 'stop', 'restart'} if target.startswith('lifecycle_') else {'activate'})
policy = json.loads(Path('/etc/coolify-node/policies/1001.json').read_text())
assert policy['version'] == 10 and policy['resources'][request['resource_id']]['kind'] == 'quadlet'
path = Path('/etc/coolify-node/release-cutpoint-' + fault_id + '.json')
witness = Path('/var/lib/coolify-node/1001/release-cutpoint-' + fault_id + '.json')

def save(record):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(record, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

with open('/etc/coolify-node/release-cutpoint.lock', 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    if path.exists():
        record = json.loads(path.read_text())
        assert record['fixture'] == fixture and record['request'] == request and record['target'] == target and record['code_hash'] == code_hash
    else:
        assert not witness.exists()
        record = {'fixture': fixture, 'fault_id': fault_id, 'target': target, 'request': request,
                  'code_hash': code_hash, 'state': 'prepared'}
        save(record)
    assert record['state'] in {'prepared', 'returned', 'interrupted'}, 'Reconcile the recorded process before reinjection'
    if record['state'] != 'interrupted':
        record['state'] = 'injecting'
        save(record)
        process = subprocess.Popen(['runuser', '-u', 'workload', '--', '/usr/bin/python3', '-I', '-c', CHILD,
                                    fixture, code_hash, fault_id, target],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        record['pid'] = process.pid
        record['process_start_ticks'] = Path('/proc/' + str(process.pid) + '/stat').read_text().split()[21]
        save(record)
        stdout, stderr = process.communicate(json.dumps(request), timeout=70)
        record.update(state='interrupted' if process.returncode in {-9, 137} else 'returned',
                      process_exit=process.returncode, stdout=stdout, stderr=stderr)
        save(record)
        assert process.returncode in {0, -9, 137}, record
    with sqlite3.connect('file:/var/lib/coolify-node/1001/journal.sqlite?mode=ro', uri=True) as db:
        row = db.execute('select state,before_state,request from operations where id=?', (request['operation_id'],)).fetchone()
    assert row and json.loads(row[2]) == request
    record['journal_state'] = row[0]
    record['before_state'] = json.loads(row[1])
    record['witness'] = json.loads(witness.read_text()) if witness.exists() else None
    print(json.dumps(record))
