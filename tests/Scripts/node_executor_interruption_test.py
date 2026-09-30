"""Kill the executor after systemd starts a restart; reconcile the SAME operation ID."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / 'storage/app/runtime-test-vm'


@unittest.skipUnless(os.environ.get('COOLIFY_RUNTIME_VM_TEST') == '1', 'Requires disposable executor fixture')
class NodeExecutorInterruptionTest(unittest.TestCase):
    def test_missing_journal_fails_closed_without_resetting_generation(self):
        record = json.loads((STATE / 'state.json').read_text())
        self.assertEqual(record['interruption_operation']['state'], 'reconciled')
        marker = record['operation_id']
        prefix = (
            'import fcntl; from pathlib import Path; '
            f'assert Path("/etc/coolify-disposable-fixture").read_text().strip() == {marker!r}; '
            'directory=Path("/var/lib/coolify-node/1001"); '
            'lock=open(directory/"executor.lock", "a"); fcntl.flock(lock,fcntl.LOCK_EX); '
            'journal=directory/"journal.sqlite"; backup=directory/"journal.loss-fixture"; '
        )

        def root(script):
            return subprocess.run([str(ROOT / 'scripts/runtime-test-vm'), 'ssh',
                                   'python3 -c ' + shlex.quote(prefix + script)], check=True, timeout=20)

        root('assert journal.is_file() and not backup.exists(); '
             'assert not (directory/"journal.sqlite-wal").exists(); journal.rename(backup)')
        try:
            request = {'protocol': 2, 'execution_uid': 1001, 'execution_gid': 1001, 'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4()),
                       'resource_id': record['executor_fixture']['resource_id'], 'action': 'status',
                       'expected_generation': 5, 'controller_epoch': 1, 'policy_version': 2,
                       'deadline': int(time.time()) + 60}
            (STATE / 'missing-journal-request.json').write_text(json.dumps(request))
            ssh = ['ssh', '-T', '-i', str(STATE / 'node_ed25519'), '-p', str(record['ssh_port']),
                   '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none', '-o', 'BatchMode=yes',
                   '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(STATE / 'known_hosts'),
                   'workload@127.0.0.1', 'coolify-node-v1']
            response = subprocess.run(ssh, input=json.dumps(request), capture_output=True, text=True, timeout=20)
            self.assertNotEqual(response.returncode, 0)
            self.assertEqual(json.loads(response.stdout)['code'], 'executor_unavailable')
        finally:
            root('assert backup.is_file() and not journal.exists(); backup.rename(journal)')
        response = subprocess.run(ssh, input=json.dumps(request), capture_output=True, text=True, timeout=20, check=True)
        self.assertEqual(json.loads(response.stdout)['generation'], 5)

    def test_lost_ssh_reply_does_not_repeat_restart(self):
        path = STATE / 'state.json'
        record = json.loads(path.read_text())
        self.assertEqual(record['executor_fixture']['state'], 'provisioned')
        marker = subprocess.check_output([str(ROOT / 'scripts/runtime-test-vm'), 'ssh', 'cat /etc/coolify-disposable-fixture'], text=True).strip()
        self.assertEqual(marker, record['operation_id'])
        request_path = STATE / 'node-interruption-request.json'

        def save():
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(record, indent=2) + '\n')
            temporary.replace(path)

        if 'interruption_operation' not in record:
            request = {'protocol': 2, 'execution_uid': 1001, 'execution_gid': 1001, 'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4()),
                       'resource_id': record['executor_fixture']['resource_id'], 'action': 'restart',
                       'expected_generation': 4, 'controller_epoch': 1, 'policy_version': 2,
                       'deadline': int(time.time()) + 600}
            request_path.write_text(json.dumps(request) + '\n')
            record['interruption_operation'] = {'id': request['operation_id'], 'state': 'preparing'}
            save()
            subprocess.run(['scp', '-i', str(STATE / 'id_ed25519'), '-P', str(record['ssh_port']),
                            '-o', 'UserKnownHostsFile=' + str(STATE / 'known_hosts'),
                            str(ROOT / 'tests/Fixtures/node-executor-interruption.py'),
                            'root@127.0.0.1:/root/node-executor-interruption.py'], check=True)
            subprocess.run([str(ROOT / 'scripts/runtime-test-vm'), 'ssh',
                            f'python3 /root/node-executor-interruption.py {marker} prepare {request["operation_id"]}'], check=True)
            record['interruption_operation']['state'] = 'prepared'
            save()
        else:
            request = json.loads(request_path.read_text())
            self.assertIn(record['interruption_operation']['state'], ['prepared', 'reconciled'],
                          'Reconcile interrupted fault injection before resuming; do not create another restart')
        ssh = ['ssh', '-T', '-i', str(STATE / 'node_ed25519'), '-p', str(record['ssh_port']),
               '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none', '-o', 'BatchMode=yes',
               '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(STATE / 'known_hosts'),
               'workload@127.0.0.1', 'coolify-node-v1']

        def deliver():
            response = subprocess.run(ssh, input=json.dumps(request), text=True, capture_output=True, check=True, timeout=45)
            return json.loads(response.stdout)

        if record['interruption_operation']['state'] == 'prepared':
            record['interruption_operation']['state'] = 'submitted'
            save()
            process = subprocess.Popen(ssh, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            process.stdin.write(json.dumps(request))
            process.stdin.close()
            process.stdin = None
            subprocess.run([str(ROOT / 'scripts/runtime-test-vm'), 'ssh',
                            f'python3 /root/node-executor-interruption.py {marker} interrupt {request["operation_id"]}'], check=True, timeout=70)
            stdout, stderr = process.communicate(timeout=45)
            self.assertNotEqual(process.returncode, 0)
            (STATE / 'node-interruption-lost-reply.json').write_text(json.dumps({'exit': process.returncode, 'stdout': stdout, 'stderr': stderr}))
            first = deliver()
            self.assertIn(first['status'], ['needs_intervention', 'succeeded'])
            (STATE / 'node-interruption-first-observation.json').write_text(json.dumps(first))
            subprocess.run([str(ROOT / 'scripts/runtime-test-vm'), 'ssh',
                            f'bash /root/rootless-quadlet-prototype.sh {marker} verify'], check=True, timeout=100)
        outcome = deliver()
        self.assertEqual(outcome['status'], 'succeeded')
        self.assertEqual(outcome['generation'], 5)
        self.assertEqual(deliver(), outcome)
        lost = json.loads((STATE / 'node-interruption-lost-reply.json').read_text())
        self.assertNotEqual(lost['exit'], 0)
        record['interruption_operation'].update(state='reconciled', outcome=outcome)
        save()


if __name__ == '__main__':
    unittest.main()
