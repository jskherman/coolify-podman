"""Actual persistent manual stop, restart interruption and stopped compensation gates."""
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


@unittest.skipUnless(os.environ.get('COOLIFY_RUNTIME_VM_TEST') == '1', 'Requires disposable repeated-boot fixture')
class NodePersistentStopTest(unittest.TestCase):
    def test_stop_intent_survives_reboots_and_failed_deployments(self):
        fixture = json.loads(subprocess.check_output([str(ROOT / 'scripts/runtime-test-vm'), 'status'], text=True))
        self.assertTrue(fixture['process_running'])
        baseline_path = STATE / 'node-persistent-stop-baseline.json'
        if baseline_path.exists():
            baseline = json.loads(baseline_path.read_text())
            self.assertEqual(baseline['state'], 'reconciled')
            self.assertEqual(baseline['fixture'], fixture['operation_id'])
        else:
            baseline = json.loads((STATE / 'node-repeat-boot-gate.json').read_text())
            self.assertEqual(baseline['state'], 'verified')
        hashes = [hashlib.sha256((ROOT / 'scripts' / name).read_bytes()).hexdigest()
                  for name in ['coolify-node-executor.py', 'coolify-node-native.py']]
        helper = ROOT / 'tests/Fixtures/node-release-cutpoint.py'
        helper_hash = hashlib.sha256(helper.read_bytes()).hexdigest()
        path = STATE / 'node-persistent-stop-gate.json'
        record = json.loads(path.read_text()) if path.exists() else {
            'state': 'prepared', 'fixture': fixture['operation_id'], 'resource_id': baseline['resource_id'],
            'hashes': hashes, 'helper_hash': helper_hash, 'initial_generation': baseline['generation'],
            'operations': {}, 'faults': {}, 'reboots': {}}
        self.assertEqual(record['fixture'], fixture['operation_id'])
        self.assertEqual(record['hashes'], hashes)
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

        def observe():
            return instrument('node-selection-observe.py', record['resource_id'])

        if record['state'] == 'prepared':
            before = observe()
            self.assertEqual(before['generation'], record['initial_generation'])
            self.assertEqual(before['operations'], [])
            self.assertIsNone(before['pending_selection'])
            record['before'] = before
            for source in [helper, ROOT / 'tests/Fixtures/node-fixture-reboot.py', ROOT / 'tests/Fixtures/node-release-continuation-upgrade.py', ROOT / 'scripts/coolify-node-executor.py', ROOT / 'scripts/coolify-node-native.py']:
                subprocess.run(['scp', '-F', '/dev/null', '-i', str(STATE / 'id_ed25519'), '-P', str(fixture['ssh_port']),
                                '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                                '-o', 'UserKnownHostsFile=' + str(STATE / 'known_hosts'), str(source),
                                'root@127.0.0.1:/root/' + source.name], check=True, timeout=25)
            if baseline['hashes'] != hashes:
                record['upgrade'] = instrument('node-release-continuation-upgrade.py', *baseline['hashes'], *hashes, record['resource_id'], str(record['initial_generation']))
                self.assertEqual(record['upgrade']['state'], 'published')
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
                if not terminal or outcome['status'] not in {'executing', 'needs_intervention'}:
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
            self.assertTrue(selection['persistent'])
            self.assertFalse(selection['volatile'])
            self.assertIsNone(selection['pending_selection'])
            self.assertEqual(selection['writer_log'], record['before']['writer_log'])
            return selection

        generation = record['initial_generation']

        def status():
            entry = operation('status_' + str(uuid.uuid4()), 'status', 0)
            return deliver(entry)

        def wait_state(active, boot_enabled, healthy=False):
            for _ in range(35):
                result = status()
                if result.get('code') == 'observation_unavailable':
                    # Container replacement can race a read; retain the denial and
                    # take a fresh observation before any subsequent mutation.
                    time.sleep(2)
                    continue
                self.assertEqual(result['status'], 'succeeded', result)
                observed = result['observed']
                if (observed['active'] == active and observed['boot_enabled'] == boot_enabled
                        and not observed['transitioning'] and (not healthy or observed['health'])):
                    return result
                time.sleep(2)
            self.fail('Observed lifecycle did not converge; preserve its recorded operation')

        def mutation(label, action, expected):
            entry = operation(label, action, expected)
            result = deliver(entry)
            self.assertEqual(result['status'], 'succeeded', result)
            self.assertEqual(result['generation'], expected + 1)
            self.assertFalse(result['observed']['failed'], 'A stop timeout must not pass the graceful lifecycle gate')
            self.assertEqual(deliver(entry), result)
            return result

        if record['state'] == 'installed':
            spec = json.loads(json.dumps(baseline['operations']['stage_compensation']['request']['spec']))
            spec['health']['path'] = '/'
            spec['command'] = ['sh', '-c', 'echo manual-stop-ready > /data/index.html; trap \'kill "$child"; wait "$child"; exit 0\' TERM INT; httpd -f -p 80 -h /data & child=$!; wait "$child"']
            staged = deliver(operation('stage_fixture', 'stage', generation, spec=spec))
            self.assertEqual(staged['status'], 'succeeded', staged)
            accepted = deliver(operation('activate_fixture', 'activate', generation, bundle_hash=staged['bundle']['bundle_hash']))
            self.assertEqual(accepted['status'], 'succeeded', accepted)
            self.assertEqual(accepted['generation'], generation + 1)
            record.update(state='fixture_ready', workload_generation=accepted['generation'],
                          workload_bundle=staged['bundle']['bundle_hash'], workload_spec=spec)
            save()
        generation = record['workload_generation']
        if record['state'] == 'fixture_ready':
            wait_state(True, True, healthy=True)
            stop = operation('stop_interrupted', 'stop', generation)
            witness = cut('stop_prepared', 'lifecycle_prepared', stop)
            self.assertIn('LoadState=loaded', witness['owner_commands'])
            self.assertIn('ExecStop={', witness['owner_commands'])
            self.assertIn('/usr/bin/podman', witness['owner_commands'])
            record['state'] = 'stop_prepared'
            save()
        if record['state'] == 'stop_prepared':
            after = reboot('prepared_stop_boot')
            self.assertEqual(after['generation'], generation)
            wait_state(False, False)
            result = deliver(record['operations']['stop_interrupted'])
            self.assertEqual(result['status'], 'succeeded', result)
            self.assertEqual(result['generation'], generation + 1)
            self.assertEqual(deliver(record['operations']['stop_interrupted']), result)
            no_op = deliver(operation('stop_no_op', 'stop', generation + 1))
            self.assertEqual(no_op['generation'], generation + 1)
            record['state'] = 'stop_recovered'
            save()
        if record['state'] == 'stop_recovered':
            mutation('start_after_stop', 'start', generation + 1)
            wait_state(True, True, healthy=True)
            restart = operation('restart_interrupted', 'restart', generation + 2)
            cut('restart_requested', 'lifecycle_requested', restart)
            record['state'] = 'restart_requested'
            save()
        if record['state'] == 'restart_requested':
            ready = wait_state(True, True, healthy=True)
            result = deliver(record['operations']['restart_interrupted'])
            self.assertEqual(result['status'], 'succeeded', result)
            self.assertEqual(result['generation'], generation + 3)
            self.assertEqual(result['observed']['invocation'], ready['observed']['invocation'])
            self.assertEqual(deliver(record['operations']['restart_interrupted']), result)
            record['state'] = 'restart_recovered'
            save()
        if record['state'] == 'restart_recovered':
            mutation('stop_for_deployment', 'stop', generation + 3)
            wait_state(False, False)
            bad = json.loads(json.dumps(record['workload_spec']))
            bad['health']['path'] = '/missing'
            staged = deliver(operation('stage_bad', 'stage', generation + 4, spec=bad))
            self.assertEqual(staged['status'], 'succeeded', staged)
            operation('activate_bad', 'activate', generation + 4, bundle_hash=staged['bundle']['bundle_hash'])
            record['state'] = 'failed_deployment_recorded'
            save()
        if record['state'] == 'failed_deployment_recorded':
            result = deliver(record['operations']['activate_bad'])
            self.assertEqual(result['status'], 'failed', result)
            self.assertEqual(result['generation'], generation + 5)
            self.assertFalse(result['observed']['active'])
            self.assertFalse(result['observed']['boot_enabled'])
            self.assertFalse(result['release']['data_recovery'])
            self.assertEqual(result['observed']['bundle_hash'], record['workload_bundle'])
            record['state'] = 'stopped_compensation_verified'
            save()
        if record['state'] == 'stopped_compensation_verified':
            after = reboot('compensated_stop_boot')
            self.assertEqual(after['generation'], generation + 5)
            wait_state(False, False)
            self.assertEqual(observe()['operations'], [])
            record['state'] = 'stop_boot_verified'
            save()
        if record['state'] == 'stop_boot_verified':
            mutation('restore_start', 'start', generation + 5)
            ready = wait_state(True, True, healthy=True)
            final = observe()
            self.assertEqual(final['operations'], [])
            self.assertEqual(final['writer_log'], record['before']['writer_log'])
            record.update(state='verified', generation=generation + 6, current_bundle=ready['observed']['bundle_hash'], final=final)
            save()
        self.assertEqual(record['state'], 'verified')


if __name__ == '__main__':
    unittest.main()
