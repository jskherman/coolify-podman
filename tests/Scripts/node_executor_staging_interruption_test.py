"""Resume one killed real staging operation through restricted SSH without rewriting it."""
import copy
import hashlib
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


@unittest.skipUnless(os.environ.get('COOLIFY_RUNTIME_VM_TEST') == '1', 'Requires disposable staging fixture')
class NodeExecutorStagingInterruptionTest(unittest.TestCase):
    def test_killed_staging_reuses_immutable_bundle_and_reconciles_same_request(self):
        fixture = json.loads(subprocess.check_output([str(ROOT / 'scripts/runtime-test-vm'), 'status'], text=True))
        self.assertTrue(fixture['process_running'])
        baseline = json.loads((STATE / 'node-staging-gate.json').read_text())
        self.assertEqual(baseline['state'], 'verified')
        marker = fixture['operation_id']
        manifest = STATE / 'node-staging-interruption-gate.json'
        source = ROOT / 'tests/Fixtures/node-executor-staging-interruption.py'
        fixture_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        if manifest.exists():
            record = json.loads(manifest.read_text())
            self.assertEqual(record['fixture'], marker)
            self.assertEqual(record['code_hash'], baseline['code_hash'])
            self.assertEqual(record['fixture_hash'], fixture_hash, 'Reconcile a changed fault-injection fixture explicitly')
        else:
            request = copy.deepcopy(baseline['request'])
            request.update(operation_id=str(uuid.uuid4()), idempotency_key=str(uuid.uuid4()), deadline=int(time.time()) + 600)
            request['spec']['command'].append('staging-fault-' + request['operation_id'])
            record = {'state': 'prepared', 'fixture': marker, 'code_hash': baseline['code_hash'],
                      'fixture_hash': fixture_hash, 'payload': {'base_operation_id': baseline['request']['operation_id'],
                                                               'request': request}}

        def save():
            temporary = manifest.with_suffix('.tmp')
            with temporary.open('w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(record, stream, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(manifest)
            descriptor = os.open(STATE, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

        save()
        ssh_options = ['-F', '/dev/null', '-T', '-p', str(fixture['ssh_port']),
                       '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none', '-o', 'BatchMode=yes',
                       '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(STATE / 'known_hosts')]
        root = ['ssh', *ssh_options, '-i', str(STATE / 'id_ed25519'), 'root@127.0.0.1']
        if record['state'] == 'prepared':
            subprocess.run(['scp', '-F', '/dev/null', '-i', str(STATE / 'id_ed25519'), '-P', str(fixture['ssh_port']),
                            '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                            '-o', 'UserKnownHostsFile=' + str(STATE / 'known_hosts'), str(source),
                            'root@127.0.0.1:/root/node-executor-staging-interruption.py'], check=True, timeout=20)
            record['state'] = 'injection_recorded'
            save()

        def instrument(phase):
            command = shlex.join(['/usr/bin/python3', '-I', '/root/node-executor-staging-interruption.py',
                                  marker, record['code_hash'], phase])
            response = subprocess.run([*root, command], input=json.dumps(record['payload']),
                                      check=False, capture_output=True, text=True, timeout=65)
            self.assertEqual(response.returncode, 0, response.stderr)
            return json.loads(response.stdout)

        if record['state'] == 'injection_recorded':
            interrupted = instrument('inject')
            self.assertIn(interrupted['journal_state'], ['executing', 'succeeded'])
            self.assertIn(interrupted['process_exit'], [-9, 137])
            record.update(state='interrupted', interrupted=interrupted)
            save()
        self.assertIn(record['state'], ['interrupted', 'verified'])
        before = instrument('inspect')
        self.assertEqual(before['witness'], record['interrupted']['witness'])
        request = record['payload']['request']
        delivery = ['ssh', *ssh_options, '-i', str(STATE / 'node_recovery_ed25519'),
                    'workload@127.0.0.1', 'coolify-node-v1']

        def deliver():
            result = subprocess.run(delivery, input=json.dumps(request), check=True,
                                    capture_output=True, text=True, timeout=60)
            return json.loads(result.stdout)

        outcome = deliver()
        self.assertEqual(outcome['status'], 'succeeded', outcome)
        self.assertEqual(outcome['operation_id'], request['operation_id'])
        self.assertEqual(outcome['resource_id'], request['resource_id'])
        self.assertEqual(outcome['generation'], 0)
        self.assertEqual(outcome['bundle'], before['witness']['bundle'])
        self.assertEqual(outcome['bundle']['state'], 'staged')
        self.assertEqual(deliver(), outcome)
        after = instrument('inspect')
        self.assertEqual(after['journal_state'], 'succeeded')
        self.assertEqual(after['journal_outcome'], outcome)
        self.assertEqual(after['witness'], before['witness'])
        self.assertEqual(after['observed'], before['observed'])
        self.assertEqual(after['observed']['load_state'], 'not-found')
        self.assertEqual(after['observed']['published_paths'], [])
        self.assertNotEqual(outcome['bundle']['bundle_hash'], baseline['outcome']['bundle']['bundle_hash'])
        record.update(state='verified', outcome=outcome, verified=after)
        save()


if __name__ == '__main__':
    unittest.main()
