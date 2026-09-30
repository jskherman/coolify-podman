"""Opt-in real phase-0 gates. Uses only the dedicated runtime-test-vm fixture."""
import json
import os
from pathlib import Path
import subprocess
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / 'storage/app/runtime-test-vm'


@unittest.skipUnless(os.environ.get('COOLIFY_RUNTIME_VM_TEST') == '1', 'Requires disposable VM')
class RuntimePrototypesTest(unittest.TestCase):
    def test_rootless_ingress_recovery_and_reboot(self):
        record = json.loads((STATE / 'state.json').read_text())
        fixture_id = record['operation_id']
        self.assertRegex(fixture_id, r'^coolify-fixture-[0-9a-f-]{36}$')

        def ssh(command):
            return subprocess.check_output(
                [str(ROOT / 'scripts/runtime-test-vm'), 'ssh', command], text=True, timeout=240,
            ).strip()

        self.assertEqual(ssh('cat /etc/coolify-disposable-fixture'), fixture_id)
        journal = STATE / 'prototype-gate.json'
        resume_observation = False
        boot_evidence = {}
        if journal.exists():
            previous = json.loads(journal.read_text())
            self.assertEqual(previous['fixture_id'], fixture_id)
            resume_observation = previous['phase'] in [
                'rootless-quadlet-prototype:verify', 'rootless-ingress-prototype:verify',
            ] and previous['state'] != 'succeeded'
            self.assertTrue(previous['state'] == 'succeeded' or resume_observation,
                            'Interrupted mutation: reconcile recorded phase and VM state before retrying')
            boot_evidence = previous.get('boot_evidence', {})

        def checkpoint(phase, state='running'):
            temporary = journal.with_suffix('.tmp')
            temporary.write_text(json.dumps({'fixture_id': fixture_id, 'phase': phase, 'state': state,
                                            'boot_evidence': boot_evidence}) + '\n')
            temporary.replace(journal)

        if not resume_observation:
            checkpoint('upload')
        for name in ['rootless-quadlet-prototype', 'rootless-ingress-prototype']:
            subprocess.run([
                'scp', '-i', str(STATE / 'id_ed25519'), '-P', str(record['ssh_port']),
                '-o', 'UserKnownHostsFile=' + str(STATE / 'known_hosts'), '-o', 'BatchMode=yes',
                str(ROOT / 'tests/Fixtures' / (name + '.sh')),
                'root@127.0.0.1:/root/' + name + '.sh',
            ], check=True, timeout=30)

        def phase(script, action, expected):
            checkpoint(script + ':' + action)
            output = ssh(f'bash /root/{script}.sh {fixture_id} {action}')
            print(output, flush=True)
            self.assertIn(expected, output)

        if resume_observation:
            phase('rootless-quadlet-prototype', 'verify', 'rootless_quadlet_verified')
            phase('rootless-ingress-prototype', 'verify', 'separate_identity_ingress_verified')
            checkpoint('verified-after-reboot', 'succeeded')
            return

        phase('rootless-quadlet-prototype', 'install', 'unchanged_reload_preserved_invocation')
        phase('rootless-quadlet-prototype', 'recover', 'failed_activation_compensated_data_preserved')
        phase('rootless-ingress-prototype', 'install', 'rejected_candidate_preserved_route')
        old_boot = ssh('cat /proc/sys/kernel/random/boot_id')
        boot_evidence['before'] = old_boot
        checkpoint('reboot-requested')
        # A lost SSH reply does not authorize a second reboot request.
        try:
            ssh('systemctl reboot')
        except subprocess.CalledProcessError:
            pass
        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            time.sleep(5)
            try:
                observed_boot = ssh('cat /proc/sys/kernel/random/boot_id')
                if observed_boot != old_boot:
                    boot_evidence['after'] = observed_boot
                    break
            except subprocess.CalledProcessError:
                continue
        else:
            self.fail('Reboot outcome unresolved; reconcile without resending reboot')
        phase('rootless-quadlet-prototype', 'verify', 'rootless_quadlet_verified')
        phase('rootless-ingress-prototype', 'verify', 'separate_identity_ingress_verified')
        checkpoint('verified-after-reboot', 'succeeded')


if __name__ == '__main__':
    unittest.main()
