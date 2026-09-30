"""Read scoped, replayable event pages over the actual restricted SSH endpoint."""
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


@unittest.skipUnless(os.environ.get('COOLIFY_RUNTIME_VM_TEST') == '1', 'Requires disposable handoff fixture')
class NodeExecutorOutboxTest(unittest.TestCase):
    def test_scoped_outbox_delivery_and_replay(self):
        fixture = json.loads((STATE / 'state.json').read_text())
        handoff = json.loads((STATE / 'node-handoff-gate.json').read_text())
        self.assertEqual(handoff['state'], 'recovered')
        marker = subprocess.check_output([str(ROOT / 'scripts/runtime-test-vm'), 'ssh', 'cat /etc/coolify-disposable-fixture'], text=True).strip()
        self.assertEqual(marker, fixture['operation_id'])
        digest = hashlib.sha256((ROOT / 'scripts/coolify-node-executor.py').read_bytes()).hexdigest()
        path = STATE / 'node-outbox-gate.json'
        record = json.loads(path.read_text()) if path.exists() else {'state': 'preparing', 'code_hash': digest, 'pages': []}
        self.assertEqual(record['code_hash'], digest)

        def save():
            temporary = path.with_suffix('.tmp')
            temporary.write_text(json.dumps(record, indent=2) + '\n')
            temporary.replace(path)

        save()
        if record['state'] == 'preparing':
            for source in [ROOT / 'scripts/coolify-node-executor.py', ROOT / 'tests/Fixtures/node-executor-outbox.py']:
                subprocess.run(['scp', '-i', str(STATE / 'id_ed25519'), '-P', str(fixture['ssh_port']),
                    '-o', 'UserKnownHostsFile=' + str(STATE / 'known_hosts'), str(source), 'root@127.0.0.1:/root/' + source.name], check=True)
            subprocess.run([str(ROOT / 'scripts/runtime-test-vm'), 'ssh', f'python3 /root/node-executor-outbox.py {marker} {digest}'], check=True)
            record['state'] = 'reading'
            save()
        ssh = ['ssh', '-T', '-p', str(fixture['ssh_port']), '-i', str(STATE / 'node_recovery_ed25519'),
               '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none', '-o', 'BatchMode=yes',
               '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(STATE / 'known_hosts'),
               'workload@127.0.0.1', 'coolify-node-v1']
        resource = fixture['executor_fixture']['resource_id']

        def deliver(request):
            result = subprocess.run(ssh, input=json.dumps(request), capture_output=True, text=True, check=True, timeout=20)
            return json.loads(result.stdout)

        cursor = 0
        all_events = []
        for page in range(10):
            if page >= len(record['pages']):
                request = {'protocol': 2, 'execution_uid': 1001, 'execution_gid': 1001, 'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4()),
                           'resource_id': resource, 'action': 'events', 'expected_generation': 0,
                           'controller_epoch': 2, 'policy_version': 4, 'deadline': int(time.time()) + 300, 'cursor': cursor}
                record['pages'].append({'request': request})
                save()
            item = record['pages'][page]
            outcome = deliver(item['request'])
            self.assertEqual(outcome['status'], 'succeeded', outcome)
            self.assertEqual(outcome['generation'], 6)
            self.assertLessEqual(len(outcome['events']), 25)
            self.assertEqual(deliver(item['request']), outcome)
            if 'outcome' in item:
                self.assertEqual(outcome, item['outcome'])
            item['outcome'] = outcome
            save()
            for event in outcome['events']:
                self.assertEqual(event['resource_id'], resource)
                self.assertGreater(event['sequence'], cursor)
                cursor = event['sequence']
                all_events.append(event)
            self.assertEqual(outcome['next_cursor'], cursor)
            if not outcome['events']:
                break
        else:
            self.fail('Outbox did not terminate within bounded fixture pages')
        self.assertTrue(any(event['payload']['operation_id'] == handoff['request']['operation_id'] for event in all_events))
        if 'foreign_request' not in record:
            record['foreign_request'] = {**record['pages'][0]['request'], 'operation_id': str(uuid.uuid4()),
                                         'idempotency_key': str(uuid.uuid4()), 'resource_id': str(uuid.uuid4())}
            save()
        self.assertEqual(deliver(record['foreign_request'])['code'], 'policy_denied')
        record.update(state='verified', event_count=len(all_events))
        save()


if __name__ == '__main__':
    unittest.main()
