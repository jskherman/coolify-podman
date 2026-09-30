"""Real unhealthy candidate, SIGKILL, kernel reboot and data-preserving recovery."""
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


@unittest.skipUnless(os.environ.get('COOLIFY_RUNTIME_VM_TEST') == '1', 'Requires disposable activation fixture')
class NodeExecutorBootTest(unittest.TestCase):
    def test_pending_candidate_is_not_boot_selected_and_recovery_keeps_data(self):
        fixture = json.loads(subprocess.check_output([str(ROOT / 'scripts/runtime-test-vm'), 'status'], text=True))
        self.assertTrue(fixture['process_running'])
        baseline = json.loads((STATE / 'node-activation-gate.json').read_text())
        self.assertEqual(baseline['state'], 'verified')
        sources = [ROOT / 'scripts/coolify-node-executor.py', ROOT / 'scripts/coolify-node-native.py']
        hashes = [hashlib.sha256(source.read_bytes()).hexdigest() for source in sources]
        manifest = STATE / 'node-boot-selection-gate.json'
        record = json.loads(manifest.read_text()) if manifest.exists() else {
            'state': 'prepared', 'fixture': fixture['operation_id'], 'resource_id': baseline['resource_id'],
            'executor_hash': hashes[0], 'adapter_hash': hashes[1], 'policy_version': 10,
            'baseline_bundle': baseline['current_bundle'], 'initial_generation': 2, 'operations': {}, 'reboots': {}}
        self.assertEqual(record['fixture'], fixture['operation_id'])
        self.assertEqual([record['executor_hash'], record['adapter_hash']], hashes,
                         'Reconcile code changes through a separate audited transition')

        def save():
            temporary = manifest.with_suffix('.tmp')
            with temporary.open('w') as stream:
                os.fchmod(stream.fileno(), 0o600)
                json.dump(record, stream, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, manifest)
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

        def instrument(filename, *arguments, payload=None, timeout=90):
            command = shlex.join(['/usr/bin/python3', '-I', '/root/' + filename, fixture['operation_id'], *arguments])
            response = subprocess.run([*root, command], input=json.dumps(payload) if payload else None,
                                      capture_output=True, text=True, timeout=timeout)
            self.assertEqual(response.returncode, 0, response.stdout + response.stderr)
            return json.loads(response.stdout)

        if record['state'] == 'prepared':
            helpers = ['node-executor-boot-selection.py', 'node-selection-observe.py',
                       'node-activation-interruption.py', 'node-fixture-reboot.py']
            for source in sources + [ROOT / 'tests/Fixtures' / name for name in helpers]:
                subprocess.run(['scp', '-F', '/dev/null', '-i', str(STATE / 'id_ed25519'), '-P', str(fixture['ssh_port']),
                                '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                                '-o', 'UserKnownHostsFile=' + str(STATE / 'known_hosts'), str(source),
                                'root@127.0.0.1:/root/' + source.name], check=True, timeout=25)
            record['installation'] = instrument('node-executor-boot-selection.py', *hashes, record['resource_id'])
            self.assertEqual(record['installation']['policy_version'], 10)
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
            for _ in range(45):
                response = subprocess.run(restricted, input=json.dumps(entry['request']),
                                          capture_output=True, text=True, timeout=90)
                self.assertEqual(response.returncode, 0, response.stdout + response.stderr)
                result = json.loads(response.stdout)
                entry['last_observed'] = result
                entry['last_stderr'] = response.stderr
                save()
                reconcilable = result['status'] == 'executing' or (
                    result['status'] == 'needs_intervention' and result.get('code') == 'activation_observation_unavailable')
                if not reconcilable or not terminal:
                    return result
                time.sleep(1)
            self.fail('Recorded operation did not converge; reconcile this ID before further mutation')

        def observe():
            return instrument('node-selection-observe.py', record['resource_id'])

        def status(label, generation):
            result = deliver(operation(label, 'status', generation))
            self.assertEqual(result['status'], 'succeeded', result)
            return result['observed']

        def reboot(label):
            if label not in record['reboots']:
                record['reboots'][label] = {'operation_id': str(uuid.uuid4()), 'before_boot': observe()['boot_id'],
                                           'state': 'prepared'}
                save()
            entry = record['reboots'][label]
            if entry['state'] == 'prepared':
                entry['state'] = 'request_recorded'
                save()
                command = shlex.join(['/usr/bin/python3', '-I', '/root/node-fixture-reboot.py',
                                      fixture['operation_id'], entry['operation_id'], entry['before_boot']])
                response = subprocess.run([*root, command], capture_output=True, text=True, timeout=20)
                entry['delivery'] = {'exit': response.returncode, 'stdout': response.stdout, 'stderr': response.stderr}
                save()
                self.assertIn(response.returncode, [0, 255], entry['delivery'])
            if entry['state'] == 'request_recorded':
                for _ in range(90):
                    try:
                        response = subprocess.run([*root, 'cat /proc/sys/kernel/random/boot_id'],
                                                  capture_output=True, text=True, timeout=10)
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
                    self.fail('Reboot outcome unresolved; do not issue another reboot')
            self.assertNotEqual(entry['before_boot'], entry['after_boot'])
            subprocess.run([str(ROOT / 'scripts/runtime-test-vm'), 'status'], check=True, capture_output=True)

        generation = record.get('initial_generation', 2)
        spec = json.loads((ROOT / 'tests/Fixtures/quadlet/prebuilt-v1/spec.json').read_text())
        spec.update(command=['sh', '-c', 'echo activation-ready > /data/index.html; exec httpd -f -p 80 -h /data'],
                    migration_classification='none')
        if record['state'] == 'installed':
            bad_spec = {**spec, 'health': {**spec['health'], 'path': '/missing'}}
            staged = deliver(operation('stage_unhealthy', 'stage', generation, spec=bad_spec))
            self.assertEqual(staged['status'], 'succeeded', staged)
            entry = operation('activate_unhealthy', 'activate', generation, bundle_hash=staged['bundle']['bundle_hash'])
            observation_offset = sum(label.startswith('unhealthy_observation_') for label in record['operations'])
            for index in range(30):
                result = deliver(entry, terminal=False)
                if result['status'] in {'succeeded', 'failed', 'denied'}:
                    break
                self.assertIn(result['status'], ['executing', 'needs_intervention'], result)
                observed = status('unhealthy_observation_' + str(observation_offset + index), generation)
                if observed['bundle_hash'] == entry['request']['bundle_hash'] and observed['transitioning']:
                    ready_offset = sum(label.startswith('unhealthy_ready_') for label in record['operations'])
                    for poll in range(8):
                        observed = status('unhealthy_ready_' + str(ready_offset + poll), generation)
                        if not observed['transitioning']:
                            break
                        time.sleep(1)
                if observed['active'] and not observed['health'] and observed['bundle_hash'] == entry['request']['bundle_hash']:
                    record['unhealthy_witness'] = observed
                    save()
                    break
            self.assertIn('unhealthy_witness', record, 'Selected files alone do not prove a live unhealthy candidate')
            compensated = deliver(entry)
            self.assertEqual(compensated['status'], 'failed', compensated)
            self.assertEqual(compensated['release']['state'], 'rolled_back')
            self.assertFalse(compensated['release']['data_recovery'])
            self.assertEqual(compensated['generation'], generation + 1)
            self.assertTrue(compensated['observed']['health'])
            self.assertEqual(compensated['observed']['bundle_hash'], record['baseline_bundle'])
            self.assertEqual(deliver(entry), compensated)
            record.update(state='unhealthy_verified', compensated=compensated)
            save()

        if record['state'] == 'unhealthy_verified':
            writer = {**spec, 'command': ['sh', '-c', 'echo boot-safe-candidate > /data/index.html; '
                                          'echo durable-write >> /data/writer.log; exec httpd -f -p 80 -h /data']}
            staged = deliver(operation('stage_writer', 'stage', generation + 1, spec=writer))
            self.assertEqual(staged['status'], 'succeeded', staged)
            operation('activate_writer', 'activate', generation + 1, bundle_hash=staged['bundle']['bundle_hash'])
            record['state'] = 'injection_recorded'
            save()
        if record['state'] == 'injection_recorded':
            entry = record['operations']['activate_writer']
            for _ in range(30):
                injected = instrument('node-activation-interruption.py', record['executor_hash'], payload=entry['request'])
                record['injection'] = injected
                save()
                if injected['state'] == 'interrupted':
                    break
                result = json.loads(injected['stdout'])
                self.assertEqual(result['status'], 'executing', result)
                time.sleep(1)
            self.assertEqual(record['injection']['process_exit'], 137)
            self.assertEqual(record['injection']['before_state']['phase'], 'activating')
            self.assertEqual(record['injection']['witness']['selection'], entry['request']['bundle_hash'])
            for index in range(30):
                observed = status('writer_observation_' + str(index), generation + 1)
                if observed['active'] and observed['health']:
                    record['writer_active'] = observed
                    break
                time.sleep(1)
            self.assertIn('writer_active', record)
            selection = observe()
            self.assertFalse(selection['persistent'])
            self.assertTrue(selection['volatile'])
            self.assertEqual(selection['pending_selection']['operation_id'], entry['request']['operation_id'])
            self.assertEqual(selection['writer_log'], 'durable-write\n')
            record.update(state='candidate_interrupted', before_reboot=selection)
            save()

        if record['state'] == 'candidate_interrupted':
            reboot('pending_candidate')
            observed = status('after_pending_reboot', generation + 1)
            self.assertFalse(observed['active'])
            self.assertIsNone(observed['bundle_hash'])
            selection = observe()
            self.assertFalse(selection['persistent'])
            self.assertFalse(selection['volatile'])
            self.assertEqual(selection['generation'], generation + 1)
            self.assertEqual(selection['writer_log'], record['before_reboot']['writer_log'])
            self.assertEqual(selection['pending_selection'], record['before_reboot']['pending_selection'])
            record.update(state='boot_isolation_verified', after_pending_reboot=selection)
            save()

        if record['state'] == 'boot_isolation_verified':
            entry = record['operations']['activate_writer']
            result = deliver(entry)
            self.assertEqual(result['status'], 'failed', result)
            self.assertEqual(result['release']['state'], 'rolled_back')
            self.assertFalse(result['release']['data_recovery'])
            self.assertEqual(result['generation'], generation + 2)
            self.assertEqual(result['observed']['bundle_hash'], record['baseline_bundle'])
            self.assertTrue(result['observed']['health'])
            self.assertEqual(deliver(entry), result)
            selection = observe()
            self.assertTrue(selection['persistent'])
            self.assertFalse(selection['volatile'])
            self.assertIsNone(selection['pending_selection'])
            self.assertEqual(selection['writer_log'], record['before_reboot']['writer_log'])
            record.update(state='recovery_verified', recovery=result, after_recovery=selection)
            save()

        if record['state'] == 'recovery_verified':
            reboot('committed_compensation')
            for index in range(30):
                observed = status('after_compensation_reboot_' + str(index), generation + 2)
                if observed['active'] and observed['health']:
                    break
                time.sleep(1)
            self.assertTrue(observed['active'])
            self.assertTrue(observed['health'])
            self.assertEqual(observed['bundle_hash'], record['baseline_bundle'])
            selection = observe()
            self.assertEqual(selection['generation'], generation + 2)
            self.assertEqual(selection['writer_log'], record['before_reboot']['writer_log'])
            self.assertTrue(selection['persistent'])
            self.assertFalse(selection['volatile'])
            self.assertIsNone(selection['pending_selection'])
            self.assertEqual(selection['operations'], [])
            record.update(state='verified', after_committed_reboot=selection, generation=generation + 2,
                          current_bundle=record['baseline_bundle'])
            save()
        self.assertEqual(record['state'], 'verified')


if __name__ == '__main__':
    unittest.main()
