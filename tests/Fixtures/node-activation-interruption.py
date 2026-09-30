#!/usr/bin/env python3
"""Disposable-only interruption after actual systemd start, before its reply."""
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
import hashlib, importlib.util, json, os, pwd, signal, sys, time
from pathlib import Path
fixture, code_hash, operation_id = sys.argv[1:]
assert sys.flags.isolated and os.getuid() == 1001 and os.getgid() == 1001
os.chdir(pwd.getpwuid(os.getuid()).pw_dir)
assert Path('/etc/coolify-disposable-fixture').read_text().strip() == fixture
source = Path('/usr/local/libexec/coolify-node-executor.py')
assert hashlib.sha256(source.read_bytes()).hexdigest() == code_hash
loader = importlib.util.spec_from_file_location('trusted_executor', source)
node = importlib.util.module_from_spec(loader)
loader.loader.exec_module(node)
request = json.load(sys.stdin)
assert request['operation_id'] == operation_id and request['action'] == 'activate'
os.umask(0o077)
runtime = node.SystemdRuntime(1001)
native = runtime.native
original_start = native.start
original_run = native.run
start_witness = {}
def observed_run(arguments, **options):
    if arguments == ['/usr/bin/systemctl', '--user', '--no-block', 'start', 'coolify-' + request['resource_id'] + '.service']:
        row = executor.db.execute('select before_state from operations where id=?', (operation_id,)).fetchone()
        start_witness.update(request_time=int(time.time()), before_request=json.loads(row[0]))
    return original_run(arguments, **options)
native.run = observed_run
def start_and_interrupt(resource_id, policy, before_request=None):
    original_start(resource_id, policy, before_request)
    witness = Path('/var/lib/coolify-node/1001/start-fault-' + operation_id + '.json')
    with witness.open('x') as stream:
        json.dump({'operation_id': operation_id, 'resource_id': resource_id,
                   'boot_id': native.boot_id(), 'selection': native.current(resource_id, policy), **start_witness}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    node.QuadletBundle.sync_directory(witness.parent)
    os.kill(os.getpid(), signal.SIGKILL)
native.start = start_and_interrupt
runtime._native = native
executor = node.Executor.open_existing(Path('/var/lib/coolify-node/1001'), runtime)
policy = lambda: node.trusted_policy(Path('/etc/coolify-node/policies/1001.json'))
try:
    print(node.canonical(executor.execute(request, 'fixture-recovery', policy)), flush=True)
finally:
    executor.db.close()
'''

fixture, code_hash = sys.argv[1:]
assert os.getuid() == 0 and sys.flags.isolated
assert Path('/etc/coolify-disposable-fixture').read_text().strip() == fixture
assert Path('/etc/coolify-node/fixture-owner').read_text() == fixture
assert hashlib.sha256(Path('/usr/local/libexec/coolify-node-executor.py').read_bytes()).hexdigest() == code_hash
request = json.load(sys.stdin)
for field in ['operation_id', 'idempotency_key', 'resource_id']:
    assert str(uuid.UUID(request[field])) == request[field]
assert request['action'] == 'activate' and request['policy_version'] == 10
policy = json.loads(Path('/etc/coolify-node/policies/1001.json').read_text())
assert policy['version'] == 10 and policy['resources'][request['resource_id']]['kind'] == 'quadlet'
record_file = Path('/etc/coolify-node/start-interruption-' + request['operation_id'] + '.json')

def save(record):
    temporary = record_file.with_suffix('.tmp')
    with temporary.open('w') as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(record, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, record_file)
    descriptor = os.open(record_file.parent, os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

with open('/etc/coolify-node/start-interruption.lock', 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    if record_file.exists():
        record = json.loads(record_file.read_text())
        assert record['fixture'] == fixture and record['request'] == request and record['code_hash'] == code_hash
    else:
        record = {'fixture': fixture, 'request': request, 'code_hash': code_hash, 'state': 'prepared'}
        save(record)
    assert record['state'] in {'prepared', 'returned', 'interrupted'}, 'Reconcile the recorded process; never blindly reinject'
    if record['state'] != 'interrupted':
        record['state'] = 'injecting'
        save(record)
        process = subprocess.Popen(['runuser', '-u', 'workload', '--', '/usr/bin/python3', '-I', '-c', CHILD,
                                    fixture, code_hash, request['operation_id']],
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
    witness = Path('/var/lib/coolify-node/1001/start-fault-' + request['operation_id'] + '.json')
    record['witness'] = json.loads(witness.read_text()) if witness.exists() else None
    print(json.dumps(record))
