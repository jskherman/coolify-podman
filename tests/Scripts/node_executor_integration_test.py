"""Real restricted-key protocol gate; mutations target only the recorded disposable unit."""
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
class NodeExecutorIntegrationTest(unittest.TestCase):
    def test_restricted_ssh_lifecycle_and_replay(self):
        fixture = json.loads((STATE / 'state.json').read_text())
        self.assertEqual(fixture['executor_fixture']['state'], 'provisioned')
        resource = fixture['executor_fixture']['resource_id']
        marker = subprocess.check_output([str(ROOT / 'scripts/runtime-test-vm'), 'ssh', 'cat /etc/coolify-disposable-fixture'], text=True).strip()
        self.assertEqual(marker, fixture['operation_id'])
        ssh = ['ssh', '-T', '-i', str(STATE / 'node_ed25519'), '-p', str(fixture['ssh_port']),
               '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none', '-o', 'BatchMode=yes',
               '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(STATE / 'known_hosts'),
               'workload@127.0.0.1']
        journal = STATE / 'node-executor-gate.json'
        steps = json.loads(journal.read_text()) if journal.exists() else {'resource': resource, 'operations': {}}
        self.assertEqual(steps['resource'], resource)

        def save():
            temporary = journal.with_suffix('.tmp')
            temporary.write_text(json.dumps(steps, indent=2) + '\n')
            temporary.replace(journal)

        def deliver(request):
            result = subprocess.run(ssh + ['coolify-node-v1'], input=json.dumps(request), text=True,
                                    capture_output=True, timeout=45, check=True)
            return json.loads(result.stdout)

        def operation(label, action, generation, changes=None):
            if label not in steps['operations']:
                request = {'protocol': 2, 'execution_uid': 1001, 'execution_gid': 1001, 'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4()),
                           'resource_id': resource, 'action': action, 'expected_generation': generation,
                           'controller_epoch': 1, 'policy_version': 1, 'deadline': int(time.time()) + 300}
                request.update(changes or {})
                steps['operations'][label] = {'request': request, 'state': 'submitted'}
                save()
            step = steps['operations'][label]
            outcome = deliver(step['request'])
            step.update(state='observed', outcome=outcome)
            save()
            return outcome

        initial = operation('initial', 'status', 1)
        self.assertEqual(initial['status'], 'succeeded', initial)
        self.assertTrue(initial['observed']['active'])
        stopped = operation('stop', 'stop', 1)
        self.assertEqual(stopped['generation'], 2, stopped)
        self.assertFalse(stopped['observed']['active'])
        self.assertEqual(deliver(steps['operations']['stop']['request']), stopped)
        self.assertEqual(operation('stale', 'start', 1)['code'], 'stale_generation')
        self.assertEqual(operation('foreign', 'start', 2, {'resource_id': str(uuid.uuid4())})['code'], 'policy_denied')
        self.assertEqual(operation('raw-command', 'start', 2, {'command': 'id'})['code'], 'invalid_request')
        self.assertEqual(operation('old-controller', 'start', 2, {'controller_epoch': 0})['code'], 'stale_controller')
        started = operation('start', 'start', 2)
        self.assertEqual(started['generation'], 3, started)
        restarted = operation('restart', 'restart', 3)
        self.assertEqual(restarted['generation'], 4, restarted)
        self.assertNotEqual(restarted['observed']['invocation'], started['observed']['invocation'])
        self.assertEqual(deliver(steps['operations']['restart']['request']), restarted)
        final = operation('final', 'status', 4)
        self.assertEqual(final['observed']['invocation'], restarted['observed']['invocation'])
        shell = subprocess.run(ssh + ['id'], input='', text=True, capture_output=True, check=True, timeout=15)
        self.assertEqual(json.loads(shell.stdout)['code'], 'restricted_transport_required')
        injected = subprocess.run(ssh + ['coolify-node-v1; id'], input='', text=True, capture_output=True, check=True, timeout=15)
        self.assertEqual(json.loads(injected.stdout)['code'], 'restricted_transport_required')
        root_login = subprocess.run(ssh[:-1] + ['root@127.0.0.1', 'id'], input='', text=True,
                                    capture_output=True, timeout=15)
        self.assertNotEqual(root_login.returncode, 0)
        self.assertIn('Permission denied', root_login.stderr)
        forward = subprocess.run(ssh[:-1] + ['-W', '127.0.0.1:18080', ssh[-1]], input='', text=True,
                                 capture_output=True, timeout=15)
        self.assertNotEqual(forward.returncode, 0)
        self.assertIn('administratively prohibited', forward.stderr)
        steps['state'] = 'succeeded'
        save()


if __name__ == '__main__':
    unittest.main()
