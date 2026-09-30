"""Real forced-SSH publication, health promotion, no-op and safe compensation."""
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

@unittest.skipUnless(os.environ.get('COOLIFY_RUNTIME_VM_TEST') == '1', 'Requires disposable staged identity fixture')
class NodeExecutorActivationTest(unittest.TestCase):
    def test_native_activation_replay_and_failed_candidate_compensation(self):
        fixture = json.loads((STATE / 'state.json').read_text())
        subprocess.run([str(ROOT / 'scripts/runtime-test-vm'), 'status'], capture_output=True, check=True)
        stage = json.loads((STATE / 'node-staging-gate.json').read_text())
        self.assertEqual(stage['state'], 'verified')
        files = ['coolify-node-executor.py', 'coolify-node-native.py']
        hashes = [hashlib.sha256((ROOT / 'scripts' / name).read_bytes()).hexdigest() for name in files]
        path = STATE / 'node-activation-gate.json'
        record = json.loads(path.read_text()) if path.exists() else {
            'state': 'preparing', 'resource_id': stage['resource_id'], 'fixture': fixture['operation_id'],
            'executor_hash': hashes[0], 'adapter_hash': hashes[1], 'operations': {}}
        self.assertEqual(record['fixture'], fixture['operation_id'])
        self.assertEqual([record['executor_hash'], record['adapter_hash']], hashes,
                         'Reconcile the installed transition before using changed code')

        def save():
            temporary = path.with_suffix('.tmp')
            with temporary.open('w') as stream:
                stream.write(json.dumps(record, indent=2) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        save()
        if record['state'] == 'preparing':
            sources = [ROOT / 'scripts' / name for name in files] + [ROOT / 'tests/Fixtures/node-executor-activation.py']
            for source in sources:
                subprocess.run(['scp', '-F', '/dev/null', '-i', str(STATE / 'id_ed25519'), '-P', str(fixture['ssh_port']),
                    '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(STATE / 'known_hosts'),
                    str(source), 'root@127.0.0.1:/root/' + source.name], check=True)
            subprocess.run([str(ROOT / 'scripts/runtime-test-vm'), 'ssh',
                f'python3 /root/node-executor-activation.py {fixture["operation_id"]} {hashes[0]} {hashes[1]} {record["resource_id"]}'], check=True)
            record['state'] = 'installed'
            save()
        ssh = ['ssh', '-F', '/dev/null', '-T', '-p', str(fixture['ssh_port']), '-i', str(STATE / 'node_recovery_ed25519'),
               '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none', '-o', 'BatchMode=yes',
               '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(STATE / 'known_hosts'),
               'workload@127.0.0.1', 'coolify-node-v1']

        def operation(label, action, generation, **fields):
            if label not in record['operations']:
                record['operations'][label] = {'request': {'protocol': 2, 'execution_uid': 1001, 'execution_gid': 1001,
                    'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4()),
                    'resource_id': record['resource_id'], 'action': action, 'expected_generation': generation,
                    'controller_epoch': 2, 'policy_version': record.get('policy_version', 7), 'deadline': int(time.time()) + 600, **fields}}
                save()
            return record['operations'][label]

        def deliver(entry, terminal=True):
            if entry['request']['policy_version'] != record.get('policy_version', 7):
                previous = entry.get('last_observed', {})
                if previous.get('status') in {'succeeded', 'failed'}:
                    return previous
                recovery = operation('recover_' + entry['request']['operation_id'] + '_p' + str(record.get('policy_version', 7)), 'recover', entry['request']['expected_generation'],
                                     target_operation_id=entry['request']['operation_id'])
                for _ in range(30):
                    result = deliver(recovery, terminal=False)
                    if result['status'] == 'succeeded':
                        entry['last_observed'] = result['recovered_operation']
                        save()
                        return result['recovered_operation']
                    self.assertEqual(result['status'], 'needs_intervention', result)
                    self.assertEqual(result['code'], 'recovery_target_unresolved', result)
                    time.sleep(1)
                self.fail('Original activation did not recover through its recorded target')
            for _ in range(30):
                response = subprocess.run(ssh, input=json.dumps(entry['request']), capture_output=True, text=True, timeout=90)
                self.assertEqual(response.returncode, 0, response.stdout + response.stderr)
                result = json.loads(response.stdout)
                entry['last_observed'] = result
                save()
                if result['status'] != 'executing' or not terminal:
                    return result
                time.sleep(1)
            self.fail('Existing activation operation did not converge in the bounded observation window')

        spec = json.loads((ROOT / 'tests/Fixtures/quadlet/prebuilt-v1/spec.json').read_text())
        spec.update(command=['sh', '-c', 'echo activation-ready > /data/index.html; exec httpd -f -p 80 -h /data'],
                    migration_classification='none')
        staged = deliver(operation('stage_healthy', 'stage', 0, spec=spec))
        self.assertEqual(staged['status'], 'succeeded', staged)
        digest = staged['bundle']['bundle_hash']
        entry = operation('activate_healthy', 'activate', 0, bundle_hash=digest)
        first = deliver(entry)
        self.assertEqual(first['status'], 'succeeded', first)
        self.assertEqual(first['release']['state'], 'committed')
        self.assertFalse(first['release']['no_op'])
        self.assertEqual(first['generation'], 1)
        self.assertTrue(first['observed']['health'])
        self.assertEqual(deliver(entry), first)
        noop = deliver(operation('healthy_noop', 'activate', 1, bundle_hash=digest))
        self.assertEqual(noop['status'], 'succeeded', noop)
        self.assertTrue(noop['release']['no_op'])
        self.assertEqual(noop['generation'], 1)
        self.assertEqual(noop['observed']['invocation'], first['observed']['invocation'])
        failed_spec = {**spec, 'health': {**spec['health'], 'path': '/missing'}}
        bad = deliver(operation('stage_unhealthy', 'stage', 1, spec=failed_spec))
        self.assertEqual(bad['status'], 'succeeded', bad)
        failed_entry = operation('activate_unhealthy', 'activate', 1, bundle_hash=bad['bundle']['bundle_hash'])
        compensated = deliver(failed_entry)
        self.assertEqual(compensated['status'], 'failed', compensated)
        self.assertEqual(compensated['release']['state'], 'rolled_back')
        self.assertFalse(compensated['release']['data_recovery'])
        self.assertEqual(compensated['release']['previous_bundle_hash'], digest)
        self.assertEqual(compensated['generation'], 2)
        self.assertTrue(compensated['observed']['health'])
        self.assertEqual(compensated['observed']['bundle_hash'], digest)
        self.assertEqual(deliver(failed_entry), compensated)
        record.update(state='verified', current_bundle=digest, generation=2)
        save()
        fixture['activation_operation'] = {'state': 'verified', 'policy_version': record.get('policy_version', 7), 'generation': 2,
            'resource_id': record['resource_id'], 'executor_sha256': hashes[0], 'adapter_sha256': hashes[1], 'bundle_hash': digest}
        temporary = STATE / 'state.tmp'
        temporary.write_text(json.dumps(fixture, indent=2) + '\n')
        os.replace(temporary, STATE / 'state.json')

if __name__ == '__main__':
    unittest.main()
