"""Real start-clock boundary, SIGKILL and health-accepted same-ID recovery."""
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


@unittest.skipUnless(os.environ.get('COOLIFY_RUNTIME_VM_TEST') == '1', 'Requires disposable boot recovery fixture')
class NodeStartBoundaryTest(unittest.TestCase):
    def test_health_budget_starts_at_real_manager_request_and_lost_reply_does_not_restart(self):
        fixture = json.loads(subprocess.check_output([str(ROOT / 'scripts/runtime-test-vm'), 'status'], text=True))
        self.assertTrue(fixture['process_running'])
        baseline = json.loads((STATE / 'node-boot-selection-gate.json').read_text())
        self.assertEqual(baseline['state'], 'verified')
        files = ['coolify-node-executor.py', 'coolify-node-native.py']
        hashes = [hashlib.sha256((ROOT / 'scripts' / name).read_bytes()).hexdigest() for name in files]
        helper = ROOT / 'tests/Fixtures/node-activation-interruption.py'
        helper_hash = hashlib.sha256(helper.read_bytes()).hexdigest()
        path = STATE / 'node-start-boundary-gate.json'
        record = json.loads(path.read_text()) if path.exists() else {
            'state': 'prepared', 'fixture': fixture['operation_id'], 'resource_id': baseline['resource_id'],
            'old_hashes': [baseline['executor_hash'], baseline['adapter_hash']], 'new_hashes': hashes,
            'helper_hash': helper_hash, 'initial_generation': baseline['generation'], 'operations': {}}
        self.assertEqual(record['fixture'], fixture['operation_id'])
        self.assertEqual(record['new_hashes'], hashes, 'Audit changes before replaying a code-bound gate')
        self.assertEqual(record['helper_hash'], helper_hash)

        def save():
            temporary = path.with_suffix('.tmp')
            with temporary.open('w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(record, stream, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            descriptor = os.open(STATE, os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

        save()
        options = ['-F', '/dev/null', '-T', '-p', str(fixture['ssh_port']), '-o', 'ConnectTimeout=5',
                   '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none', '-o', 'BatchMode=yes',
                   '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(STATE / 'known_hosts')]
        root = ['ssh', *options, '-i', str(STATE / 'id_ed25519'), 'root@127.0.0.1']
        restricted = ['ssh', *options, '-i', str(STATE / 'node_recovery_ed25519'),
                      'workload@127.0.0.1', 'coolify-node-v1']

        def instrument(name, *arguments, payload=None):
            command = shlex.join(['/usr/bin/python3', '-I', '/root/' + name, fixture['operation_id'], *arguments])
            result = subprocess.run([*root, command], input=json.dumps(payload) if payload else None,
                                    text=True, capture_output=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)

        if record['state'] == 'prepared':
            sources = [ROOT / 'scripts' / name for name in files] + [helper,
                ROOT / 'tests/Fixtures/node-release-continuation-upgrade.py']
            for source in sources:
                subprocess.run(['scp', '-F', '/dev/null', '-i', str(STATE / 'id_ed25519'), '-P', str(fixture['ssh_port']),
                                '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                                '-o', 'UserKnownHostsFile=' + str(STATE / 'known_hosts'), str(source),
                                'root@127.0.0.1:/root/' + source.name], check=True, timeout=25)
            record['upgrade'] = instrument('node-release-continuation-upgrade.py',
                *record['old_hashes'], *hashes, record['resource_id'], str(record['initial_generation']))
            record['state'] = 'installed'
            save()

        def operation(label, action, **fields):
            if label not in record['operations']:
                record['operations'][label] = {'request': {
                    'protocol': 2, 'execution_uid': 1001, 'execution_gid': 1001,
                    'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4()),
                    'resource_id': record['resource_id'], 'action': action, 'expected_generation': record['initial_generation'],
                    'controller_epoch': 2, 'policy_version': 10, 'deadline': int(time.time()) + 600, **fields}}
                save()
            return record['operations'][label]

        def deliver(entry):
            result = subprocess.run(restricted, input=json.dumps(entry['request']), text=True, capture_output=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            entry['outcome'] = json.loads(result.stdout)
            entry['stderr'] = result.stderr
            save()
            return entry['outcome']

        if record['state'] == 'installed':
            spec = json.loads((ROOT / 'tests/Fixtures/quadlet/prebuilt-v1/spec.json').read_text())
            spec.update(command=['sh', '-c', 'echo start-boundary-ready > /data/index.html; exec httpd -f -p 80 -h /data'],
                        migration_classification='none')
            staged = deliver(operation('stage', 'stage', spec=spec))
            self.assertEqual(staged['status'], 'succeeded', staged)
            operation('activate', 'activate', bundle_hash=staged['bundle']['bundle_hash'])
            record['state'] = 'injection_recorded'
            save()
        if record['state'] == 'injection_recorded':
            for _ in range(30):
                result = instrument('node-activation-interruption.py', hashes[0], payload=record['operations']['activate']['request'])
                record['injection'] = result
                save()
                if result['state'] == 'interrupted':
                    break
                outcome = json.loads(result['stdout'])
                self.assertIn(outcome['status'], ['executing', 'needs_intervention'], outcome)
                time.sleep(1)
            self.assertIn(record['injection']['process_exit'], [-9, 137])
            witness = record['injection']['witness']
            self.assertEqual(witness['before_request']['phase'], 'activating')
            self.assertGreaterEqual(witness['request_time'] - witness['before_request']['activation_started_at'], 0)
            self.assertLessEqual(witness['request_time'] - witness['before_request']['activation_started_at'], 1)
            record['state'] = 'interrupted'
            save()
        if record['state'] == 'interrupted':
            offset = sum(label.startswith('status_') for label in record['operations'])
            for index in range(30):
                result = deliver(operation('status_' + str(offset + index), 'status'))
                self.assertEqual(result['status'], 'succeeded', result)
                observed = result['observed']
                if observed['active'] and observed['health']:
                    break
                time.sleep(1)
            self.assertTrue(observed['active'])
            self.assertTrue(observed['health'])
            self.assertEqual(observed['bundle_hash'], record['operations']['activate']['request']['bundle_hash'])
            record.update(state='health_observed', before_recovery=observed)
            save()
        if record['state'] == 'health_observed':
            result = deliver(record['operations']['activate'])
            self.assertEqual(result['status'], 'succeeded', result)
            self.assertEqual(result['generation'], record['initial_generation'] + 1)
            self.assertFalse(result['release']['no_op'])
            self.assertEqual(result['observed']['invocation'], record['before_recovery']['invocation'])
            self.assertEqual(deliver(record['operations']['activate']), result)
            selection = instrument('node-selection-observe.py', record['resource_id'])
            self.assertTrue(selection['persistent'])
            self.assertFalse(selection['volatile'])
            self.assertIsNone(selection['pending_selection'])
            self.assertEqual(selection['operations'], [])
            record.update(state='verified', generation=result['generation'], current_bundle=result['observed']['bundle_hash'], final=selection)
            save()
        self.assertEqual(record['state'], 'verified')


if __name__ == '__main__':
    unittest.main()
