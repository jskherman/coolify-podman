"""Actual repeated kernel loss during accepted restart and safe compensation."""
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


@unittest.skipUnless(os.environ.get('COOLIFY_RUNTIME_VM_TEST') == '1', 'Requires disposable start-boundary fixture')
class NodeRepeatBootTest(unittest.TestCase):
    def test_accepted_and_compensating_releases_recover_across_two_boots_each(self):
        fixture = json.loads(subprocess.check_output([str(ROOT / 'scripts/runtime-test-vm'), 'status'], text=True))
        self.assertTrue(fixture['process_running'])
        baseline = json.loads((STATE / 'node-start-boundary-gate.json').read_text())
        self.assertEqual(baseline['state'], 'verified')
        hashes = [hashlib.sha256((ROOT / 'scripts' / name).read_bytes()).hexdigest()
                  for name in ['coolify-node-executor.py', 'coolify-node-native.py']]
        helper = ROOT / 'tests/Fixtures/node-release-cutpoint.py'
        helper_hash = hashlib.sha256(helper.read_bytes()).hexdigest()
        path = STATE / 'node-repeat-boot-gate.json'
        record = json.loads(path.read_text()) if path.exists() else {
            'state': 'prepared', 'fixture': fixture['operation_id'], 'resource_id': baseline['resource_id'],
            'hashes': hashes, 'helper_hash': helper_hash, 'initial_generation': baseline['generation'],
            'operations': {}, 'faults': {}, 'reboots': {}}
        self.assertEqual(record['fixture'], fixture['operation_id'])
        self.assertEqual(record['hashes'], hashes)
        self.assertEqual(record['helper_hash'], helper_hash)
        self.assertEqual(baseline['new_hashes'], hashes)

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

        def observe():
            return instrument('node-selection-observe.py', record['resource_id'])

        if record['state'] == 'prepared':
            before = observe()
            self.assertEqual(before['generation'], record['initial_generation'])
            self.assertEqual(before['operations'], [])
            self.assertIsNone(before['pending_selection'])
            record['before'] = before
            for source in [helper, ROOT / 'tests/Fixtures/node-fixture-reboot.py']:
                subprocess.run(['scp', '-F', '/dev/null', '-i', str(STATE / 'id_ed25519'), '-P', str(fixture['ssh_port']),
                                '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                                '-o', 'UserKnownHostsFile=' + str(STATE / 'known_hosts'), str(source),
                                'root@127.0.0.1:/root/' + source.name], check=True, timeout=25)
            record['state'] = 'installed'
            save()

        def operation(label, action, generation, **fields):
            if label not in record['operations']:
                record['operations'][label] = {'request': {
                    'protocol': 2, 'execution_uid': 1001, 'execution_gid': 1001,
                    'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4()),
                    'resource_id': record['resource_id'], 'action': action, 'expected_generation': generation,
                    'controller_epoch': 2, 'policy_version': 10, 'deadline': int(time.time()) + 1800, **fields}}
                save()
            return record['operations'][label]

        def deliver(entry, terminal=True):
            for _ in range(40):
                result = subprocess.run(restricted, input=json.dumps(entry['request']), text=True, capture_output=True, timeout=90)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                entry['outcome'] = json.loads(result.stdout)
                entry['stderr'] = result.stderr
                save()
                outcome = entry['outcome']
                if not terminal or outcome['status'] != 'executing':
                    return outcome
                time.sleep(1)
            self.fail('Existing operation remains unresolved; preserve its ID')

        def cut(label, target, entry):
            if label not in record['faults']:
                record['faults'][label] = {'fault_id': str(uuid.uuid4()), 'target': target,
                                          'operation_id': entry['request']['operation_id']}
                save()
            fault = record['faults'][label]
            for _ in range(40):
                result = instrument('node-release-cutpoint.py', hashes[0], fault['fault_id'], target, payload=entry['request'])
                fault['observed'] = result
                save()
                if result['state'] == 'interrupted':
                    break
                outcome = json.loads(result['stdout'])
                self.assertIn(outcome['status'], ['executing', 'needs_intervention'], outcome)
                time.sleep(1)
            self.assertIn(fault['observed']['process_exit'], [-9, 137])
            self.assertEqual(fault['observed']['witness']['target'], target)
            self.assertEqual(fault['observed']['witness']['operation_id'], entry['request']['operation_id'])
            return fault['observed']['witness']

        def reboot(label):
            if label not in record['reboots']:
                record['reboots'][label] = {'operation_id': str(uuid.uuid4()), 'before_boot': observe()['boot_id'], 'state': 'prepared'}
                save()
            entry = record['reboots'][label]
            if entry['state'] == 'prepared':
                entry['state'] = 'request_recorded'
                save()
                command = shlex.join(['/usr/bin/python3', '-I', '/root/node-fixture-reboot.py',
                                      fixture['operation_id'], entry['operation_id'], entry['before_boot']])
                response = subprocess.run([*root, command], text=True, capture_output=True, timeout=20)
                entry['delivery'] = {'exit': response.returncode, 'stdout': response.stdout, 'stderr': response.stderr}
                save()
                self.assertIn(response.returncode, [0, 255], entry['delivery'])
            if entry['state'] == 'request_recorded':
                for _ in range(90):
                    try:
                        response = subprocess.run([*root, 'cat /proc/sys/kernel/random/boot_id'],
                                                  text=True, capture_output=True, timeout=10)
                    except subprocess.TimeoutExpired:
                        entry['poll_timeouts'] = entry.get('poll_timeouts', 0) + 1
                        save()
                        time.sleep(2)
                        continue
                    if response.returncode == 0 and response.stdout.strip() != entry['before_boot']:
                        entry.update(state='boot_observed', after_boot=response.stdout.strip())
                        save()
                        break
                    time.sleep(2)
                else:
                    self.fail('Recorded reboot is unresolved; never issue another reboot blindly')
            self.assertNotEqual(entry['before_boot'], entry['after_boot'])
            subprocess.run([str(ROOT / 'scripts/runtime-test-vm'), 'status'], check=True, capture_output=True)
            selection = observe()
            self.assertFalse(selection['persistent'])
            self.assertFalse(selection['volatile'])
            self.assertIsNotNone(selection['pending_selection'])
            self.assertEqual(selection['writer_log'], record['before']['writer_log'])
            return selection

        generation = record['initial_generation']
        spec = json.loads((ROOT / 'tests/Fixtures/quadlet/prebuilt-v1/spec.json').read_text())
        spec.update(command=['sh', '-c', 'echo repeat-boot-accepted > /data/index.html; exec httpd -f -p 80 -h /data'],
                    migration_classification='none')
        if record['state'] == 'installed':
            staged = deliver(operation('stage_accepted', 'stage', generation, spec=spec))
            self.assertEqual(staged['status'], 'succeeded', staged)
            operation('activate_accepted', 'activate', generation, bundle_hash=staged['bundle']['bundle_hash'])
            record['state'] = 'acceptance_fault_recorded'
            save()
        accepted = record['operations']['activate_accepted']
        if record['state'] == 'acceptance_fault_recorded':
            witness = cut('accepted_before_publication', 'accepted_before_publication', accepted)
            self.assertEqual(witness['before_state']['phase'], 'committed')
            self.assertFalse(witness['persistent'])
            self.assertTrue(witness['volatile'])
            record['state'] = 'accepted_unpublished'
            save()
        if record['state'] == 'accepted_unpublished':
            after = reboot('accepted_first_boot')
            self.assertEqual(after['generation'], generation)
            witness = cut('accepted_restart', 'accepted_restart', accepted)
            self.assertEqual(witness['before_state']['phase'], 'accepted_restart')
            self.assertEqual(witness['selection'], accepted['request']['bundle_hash'])
            record['state'] = 'accepted_restart_interrupted'
            save()
        if record['state'] == 'accepted_restart_interrupted':
            after = reboot('accepted_second_boot')
            self.assertEqual(after['generation'], generation)
            witness = cut('accepted_after_publication', 'accepted_after_publication', accepted)
            self.assertTrue(witness['persistent'])
            self.assertFalse(witness['volatile'])
            record['state'] = 'accepted_published_unfinished'
            save()
        if record['state'] == 'accepted_published_unfinished':
            result = deliver(accepted)
            self.assertEqual(result['status'], 'succeeded', result)
            self.assertEqual(result['generation'], generation + 1)
            self.assertTrue(result['observed']['health'])
            self.assertEqual(deliver(accepted), result)
            self.assertEqual(observe()['operations'], [])
            record['state'] = 'accepted_verified'
            save()
        if record['state'] == 'accepted_verified':
            bad = {**spec, 'health': {**spec['health'], 'path': '/missing'}}
            staged = deliver(operation('stage_compensation', 'stage', generation + 1, spec=bad))
            self.assertEqual(staged['status'], 'succeeded', staged)
            operation('activate_compensation', 'activate', generation + 1, bundle_hash=staged['bundle']['bundle_hash'])
            record['state'] = 'compensation_fault_recorded'
            save()
        if record['state'] == 'compensation_fault_recorded':
            entry = record['operations']['activate_compensation']
            witness = cut('compensation_first_start', 'compensating_start', entry)
            self.assertEqual(witness['selection'], accepted['request']['bundle_hash'])
            self.assertEqual(witness['before_state']['phase'], 'compensating_start')
            record['state'] = 'compensation_started_interrupted'
            save()
        if record['state'] == 'compensation_started_interrupted':
            entry = record['operations']['activate_compensation']
            after = reboot('compensation_first_boot')
            self.assertEqual(after['generation'], generation + 1)
            witness = cut('compensation_second_start', 'compensating_start', entry)
            self.assertEqual(witness['selection'], accepted['request']['bundle_hash'])
            self.assertEqual(witness['before_state']['phase'], 'compensating_start')
            record['state'] = 'compensation_restarted_interrupted'
            save()
        if record['state'] == 'compensation_restarted_interrupted':
            after = reboot('compensation_second_boot')
            self.assertEqual(after['generation'], generation + 1)
            record['state'] = 'compensation_boot_verified'
            save()
        if record['state'] == 'compensation_boot_verified':
            entry = record['operations']['activate_compensation']
            result = deliver(entry)
            self.assertEqual(result['status'], 'failed', result)
            self.assertEqual(result['release']['state'], 'rolled_back')
            self.assertFalse(result['release']['data_recovery'])
            self.assertEqual(result['generation'], generation + 2)
            self.assertTrue(result['observed']['health'])
            self.assertEqual(result['observed']['bundle_hash'], accepted['request']['bundle_hash'])
            self.assertEqual(deliver(entry), result)
            final = observe()
            self.assertTrue(final['persistent'])
            self.assertFalse(final['volatile'])
            self.assertIsNone(final['pending_selection'])
            self.assertEqual(final['operations'], [])
            self.assertEqual(final['writer_log'], record['before']['writer_log'])
            record.update(state='verified', generation=result['generation'], current_bundle=result['observed']['bundle_hash'], final=final)
            save()
        self.assertEqual(record['state'], 'verified')


if __name__ == '__main__':
    unittest.main()
