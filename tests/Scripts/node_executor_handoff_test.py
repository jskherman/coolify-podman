"""Fence the old credential and recover its interrupted effect under a new owner."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / 'storage/app/runtime-test-vm'


@unittest.skipUnless(os.environ.get('COOLIFY_RUNTIME_VM_TEST') == '1', 'Requires disposable executor fixture')
class NodeExecutorHandoffTest(unittest.TestCase):
    def test_ownership_handoff_recovers_without_repeating_the_old_effect(self):
        fixture = json.loads((STATE / 'state.json').read_text())
        self.assertEqual(fixture['interruption_operation']['state'], 'reconciled')
        marker = subprocess.check_output([str(ROOT / 'scripts/runtime-test-vm'), 'ssh', 'cat /etc/coolify-disposable-fixture'], text=True).strip()
        self.assertEqual(marker, fixture['operation_id'])
        manifest = STATE / 'node-handoff-gate.json'
        digest = hashlib.sha256((ROOT / 'scripts/coolify-node-executor.py').read_bytes()).hexdigest()

        def save():
            temporary = manifest.with_suffix('.tmp')
            temporary.write_text(json.dumps(record, indent=2) + '\n')
            temporary.replace(manifest)

        if manifest.exists():
            record = json.loads(manifest.read_text())
            self.assertEqual(record['code_hash'], digest)
        else:
            self.assertFalse((STATE / 'node_recovery_ed25519').exists(), 'Reconcile an existing key before provisioning')
            request = {'protocol': 2, 'execution_uid': 1001, 'execution_gid': 1001, 'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4()),
                       'resource_id': fixture['executor_fixture']['resource_id'], 'action': 'restart',
                       'expected_generation': 5, 'controller_epoch': 1, 'policy_version': 2, 'deadline': int(time.time()) + 600}
            recovery = {**request, 'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4()),
                        'action': 'recover', 'target_operation_id': request['operation_id'],
                        'controller_epoch': 2, 'policy_version': 3}
            record = {'state': 'preparing', 'code_hash': digest, 'request': request, 'recovery': recovery}
            save()
            subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(STATE / 'node_recovery_ed25519')], check=True)

        def root(phase):
            return subprocess.check_output([str(ROOT / 'scripts/runtime-test-vm'), 'ssh',
                f'python3 /root/node-executor-handoff.py {marker} {phase} {record["request"]["operation_id"]} {digest}'], text=True, timeout=70)

        if record['state'] == 'preparing':
            for source in [ROOT / 'scripts/coolify-node-executor.py', ROOT / 'tests/Fixtures/node-executor-handoff.py', STATE / 'node_recovery_ed25519.pub']:
                subprocess.run(['scp', '-i', str(STATE / 'id_ed25519'), '-P', str(fixture['ssh_port']),
                    '-o', 'UserKnownHostsFile=' + str(STATE / 'known_hosts'), str(source), 'root@127.0.0.1:/root/' + source.name], check=True)
            root('prepare')
            record['state'] = 'prepared'
            save()
        base = ['ssh', '-T', '-p', str(fixture['ssh_port']), '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none',
                '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(STATE / 'known_hosts')]
        old = base + ['-i', str(STATE / 'node_ed25519'), 'workload@127.0.0.1', 'coolify-node-v1']
        new = base + ['-i', str(STATE / 'node_recovery_ed25519'), 'workload@127.0.0.1', 'coolify-node-v1']
        if record['state'] == 'prepared':
            record['state'] = 'submitted'
            save()
            process = subprocess.Popen(old, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            process.stdin.write(json.dumps(record['request']))
            process.stdin.close()
            root('interrupt')
            self.assertNotEqual(process.wait(timeout=15), 0)
            process.stdout.close()
            process.stderr.close()
            record['state'] = 'interrupted'
            save()
        if record['state'] == 'submitted':
            observed = json.loads(root('prepare'))
            self.assertIn(observed['state'], ['interrupted', 'transferred'], 'Inspect interrupted fault injection; never submit another restart')
            record['state'] = observed['state']
            save()
        if record['state'] == 'interrupted':
            root('transfer')
            record['state'] = 'transferred'
            save()
        self.assertIn(record['state'], ['transferred', 'recovered'])
        root('ready')
        denied = subprocess.run(old, input=json.dumps(record['request']), capture_output=True, text=True, check=True, timeout=20)
        self.assertEqual(json.loads(denied.stdout)['code'], 'policy_denied')
        for attempt in range(2):
            response = subprocess.run(new, input=json.dumps(record['recovery']), capture_output=True, text=True, check=True, timeout=30)
            outcome = json.loads(response.stdout)
            self.assertEqual(outcome['status'], 'succeeded', outcome)
            self.assertEqual(outcome['generation'], 6)
            self.assertEqual(outcome['recovered_operation']['operation_id'], record['request']['operation_id'])
            self.assertEqual(outcome['recovered_operation']['generation'], 6)
            self.assertTrue(outcome['observed']['active'])
            if 'outcome' in record:
                self.assertEqual(outcome, record['outcome'])
            record.update(state='recovered', outcome=outcome)
            save()


if __name__ == '__main__':
    unittest.main()
