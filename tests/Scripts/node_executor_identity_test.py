"""Identity reassignment must be denied before any lifecycle effect."""
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


@unittest.skipUnless(os.environ.get('COOLIFY_RUNTIME_VM_TEST') == '1', 'Requires disposable outbox fixture')
class NodeExecutorIdentityTest(unittest.TestCase):
    def test_uid_gid_binding_and_legacy_protocol_fence(self):
        fixture = json.loads((STATE / 'state.json').read_text())
        outbox = json.loads((STATE / 'node-outbox-gate.json').read_text())
        self.assertEqual(outbox['state'], 'verified')
        marker = subprocess.check_output([str(ROOT / 'scripts/runtime-test-vm'), 'ssh', 'cat /etc/coolify-disposable-fixture'], text=True).strip()
        self.assertEqual(marker, fixture['operation_id'])
        digest = hashlib.sha256((ROOT / 'scripts/coolify-node-executor.py').read_bytes()).hexdigest()
        manifest = STATE / 'node-identity-gate.json'
        record = json.loads(manifest.read_text()) if manifest.exists() else {'state': 'preparing', 'code_hash': digest}
        self.assertEqual(record['code_hash'], digest)

        def save():
            temporary = manifest.with_suffix('.tmp')
            temporary.write_text(json.dumps(record, indent=2) + '\n')
            temporary.replace(manifest)

        save()
        if record['state'] == 'preparing':
            for source in [ROOT / 'scripts/coolify-node-executor.py', ROOT / 'tests/Fixtures/node-executor-identity.py']:
                subprocess.run(['scp', '-i', str(STATE / 'id_ed25519'), '-P', str(fixture['ssh_port']),
                    '-o', 'UserKnownHostsFile=' + str(STATE / 'known_hosts'), str(source), 'root@127.0.0.1:/root/' + source.name], check=True)
            subprocess.run([str(ROOT / 'scripts/runtime-test-vm'), 'ssh', f'python3 /root/node-executor-identity.py {marker} {digest}'], check=True)
            record['state'] = 'installed'
            save()
        ssh = ['ssh', '-T', '-p', str(fixture['ssh_port']), '-i', str(STATE / 'node_recovery_ed25519'),
               '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none', '-o', 'BatchMode=yes',
               '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(STATE / 'known_hosts'),
               'workload@127.0.0.1', 'coolify-node-v1']
        if 'request' not in record:
            record['request'] = {'protocol': 2, 'execution_uid': 1001, 'execution_gid': 1001,
                'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4()),
                'resource_id': fixture['executor_fixture']['resource_id'], 'action': 'status',
                'expected_generation': 0, 'controller_epoch': 2, 'policy_version': 5, 'deadline': int(time.time()) + 300}
            record['denied_requests'] = []
            for changed in [{'execution_uid': 1002}, {'execution_gid': 1002}, {'protocol': 1}]:
                request = {**record['request'], 'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4()),
                           'action': 'restart', 'expected_generation': 6, **changed}
                if request['protocol'] == 1:
                    del request['execution_uid']
                    del request['execution_gid']
                record['denied_requests'].append(request)
            save()

        def deliver(request):
            response = subprocess.run(ssh, input=json.dumps(request), capture_output=True, text=True, check=True, timeout=20)
            return json.loads(response.stdout)

        first = deliver(record['request'])
        self.assertEqual(first['status'], 'succeeded', first)
        self.assertEqual(first['generation'], 6)
        for request, code in zip(record['denied_requests'], ['runtime_identity_mismatch', 'runtime_identity_mismatch', 'unsupported_protocol']):
            self.assertEqual(deliver(request)['code'], code)
        current = {**record['request'], 'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4()), 'deadline': int(time.time()) + 60}
        record['verification_request'] = current
        save()
        self.assertEqual(deliver(current)['observed']['invocation'], first['observed']['invocation'])
        record.update(state='verified', outcome=first)
        save()


if __name__ == '__main__':
    unittest.main()
