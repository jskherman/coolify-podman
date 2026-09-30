"""Real restricted-SSH staging: pinned generator, policy denial and replay without activation."""
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


@unittest.skipUnless(os.environ.get('COOLIFY_RUNTIME_VM_TEST') == '1', 'Requires disposable identity fixture')
class NodeExecutorStagingTest(unittest.TestCase):
    def test_stage_validate_replay_and_policy_denial_without_activation(self):
        fixture = json.loads((STATE / 'state.json').read_text())
        previous = json.loads((STATE / 'node-identity-gate.json').read_text())
        self.assertEqual(previous['state'], 'verified')
        marker = subprocess.check_output([str(ROOT / 'scripts/runtime-test-vm'), 'ssh', 'cat /etc/coolify-disposable-fixture'], text=True).strip()
        self.assertEqual(marker, fixture['operation_id'])
        digest = hashlib.sha256((ROOT / 'scripts/coolify-node-executor.py').read_bytes()).hexdigest()
        manifest = STATE / 'node-staging-gate.json'
        record = json.loads(manifest.read_text()) if manifest.exists() else {
            'state': 'preparing', 'code_hash': digest, 'resource_id': str(uuid.uuid4())}
        self.assertEqual(record['code_hash'], digest, 'Reconcile the installed version before running a changed fixture')

        def save():
            temporary = manifest.with_suffix('.tmp')
            with temporary.open('w') as stream:
                stream.write(json.dumps(record, indent=2) + '\n')
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(manifest)

        save()
        if record['state'] == 'preparing':
            for source in [ROOT / 'scripts/coolify-node-executor.py', ROOT / 'tests/Fixtures/node-executor-staging.py']:
                subprocess.run(['scp', '-F', '/dev/null', '-i', str(STATE / 'id_ed25519'), '-P', str(fixture['ssh_port']),
                    '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(STATE / 'known_hosts'),
                    str(source), 'root@127.0.0.1:/root/' + source.name], check=True)
            subprocess.run([str(ROOT / 'scripts/runtime-test-vm'), 'ssh',
                f'python3 /root/node-executor-staging.py {marker} {digest} {record["resource_id"]}'], check=True)
            record['state'] = 'installed'
            save()
        ssh = ['ssh', '-F', '/dev/null', '-T', '-p', str(fixture['ssh_port']), '-i', str(STATE / 'node_recovery_ed25519'),
               '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none', '-o', 'BatchMode=yes',
               '-o', 'StrictHostKeyChecking=yes', '-o', 'UserKnownHostsFile=' + str(STATE / 'known_hosts'),
               'workload@127.0.0.1', 'coolify-node-v1']
        if 'request' not in record:
            spec = json.loads((ROOT / 'tests/Fixtures/quadlet/prebuilt-v1-unicode/spec.json').read_text())
            record['request'] = {'protocol': 2, 'execution_uid': 1001, 'execution_gid': 1001,
                'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4()),
                'resource_id': record['resource_id'], 'action': 'stage', 'spec': spec,
                'expected_generation': 0, 'controller_epoch': 2, 'policy_version': 6, 'deadline': int(time.time()) + 600}
            record['denied_requests'] = []
            for changed in [{'spec': {**spec, 'privileged': True}},
                            {'spec': {**spec, 'ports': [{'bind': '0.0.0.0', 'host': 18100, 'container': 80}]}},
                            {'files': {'foreign.service': '[Service]'}},
                            {'resource_id': str(uuid.uuid4())}]:
                record['denied_requests'].append({**record['request'], 'operation_id': str(uuid.uuid4()),
                                                 'idempotency_key': str(uuid.uuid4()), **changed})
            save()

        def deliver(request):
            response = subprocess.run(ssh, input=json.dumps(request), capture_output=True, text=True, check=True, timeout=60)
            return json.loads(response.stdout)

        first = deliver(record['request'])
        self.assertEqual(first['status'], 'succeeded', first)
        self.assertEqual(first['bundle']['state'], 'staged')
        self.assertEqual(first['generation'], 0)
        self.assertEqual(first, deliver(record['request']))
        for request, code in zip(record['denied_requests'], ['unsupported_spec', 'unsupported_spec', 'invalid_request', 'policy_denied']):
            self.assertEqual(deliver(request)['code'], code)
        inspection = (
            'import json,subprocess; from pathlib import Path; '
            f'assert Path("/etc/coolify-disposable-fixture").read_text().strip()=={marker!r}; '
            f'p=Path("/var/lib/coolify-node/1001/releases/{record["resource_id"]}/{first["bundle"]["bundle_hash"]}"); '
            'm=json.loads((p/"manifest.json").read_text()); '
            'print(json.dumps({"manifest":m,"mode":oct(p.stat().st_mode & 0o777),'
            f'"selected":Path("/home/workload/.config/containers/systemd/coolify-{record["resource_id"]}").exists(),'
            f'"unit":subprocess.run(["runuser","-u","workload","--","env","XDG_RUNTIME_DIR=/run/user/1001","systemctl","--user","show","coolify-{record["resource_id"]}.service","--property=LoadState","--value"],capture_output=True,text=True).stdout.strip()}}))'
        )
        actual = json.loads(subprocess.check_output([str(ROOT / 'scripts/runtime-test-vm'), 'ssh',
            'python3 -c ' + shlex.quote(inspection)], text=True))
        self.assertEqual(actual['manifest']['metadata'], first['bundle'])
        self.assertEqual(actual['manifest']['spec'], record['request']['spec'])
        self.assertEqual(actual['mode'], '0o700')
        self.assertFalse(actual['selected'])
        self.assertEqual(actual['unit'], 'not-found')
        record.update(state='verified', outcome=first)
        save()
        fixture['staging_operation'] = {'state': 'verified', 'operation_id': record['request']['operation_id'],
            'resource_id': record['resource_id'], 'policy_version': 6, 'controller_epoch': 2,
            'executor_sha256': digest, 'bundle_hash': first['bundle']['bundle_hash']}
        temporary = STATE / 'state.tmp'
        temporary.write_text(json.dumps(fixture, indent=2) + '\n')
        temporary.replace(STATE / 'state.json')


if __name__ == '__main__':
    unittest.main()
