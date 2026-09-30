"""Node compilation must independently enforce policy and match the PHP golden bundle."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
loader = importlib.util.spec_from_file_location('node', ROOT / 'scripts/coolify-node-executor.py')
node = importlib.util.module_from_spec(loader)
loader.loader.exec_module(node)
RESOURCE = '11111111-1111-4111-8111-111111111111'


class QuadletBundleTest(unittest.TestCase):
    def setUp(self):
        self.spec = json.loads((ROOT / 'tests/Fixtures/quadlet/prebuilt-v1/spec.json').read_text())
        self.policy = {'kind': 'quadlet', 'profile': 'prebuilt-v1', 'generation': 0,
                       'host_ports': [18100], 'image_registries': ['docker.io'],
                       'max_memory_mib': 256, 'max_cpu_millis': 1000, 'max_volumes': 2}

    def test_node_compiles_the_reviewed_golden_bundle(self):
        files = node.QuadletBundle.compile(self.spec, RESOURCE, self.policy)
        for suffix, fixture in [('container', 'application.container'), ('network', 'application.network')]:
            self.assertEqual(files[f'coolify-{RESOURCE}.{suffix}'],
                             (ROOT / 'tests/Fixtures/quadlet/prebuilt-v1' / fixture).read_text())
        self.assertEqual(files[f'coolify-{RESOURCE}-data.volume'],
                         (ROOT / 'tests/Fixtures/quadlet/prebuilt-v1/data.volume').read_text())

    def test_node_denies_schema_and_policy_bypass_independent_of_controller(self):
        invalid = [dict(privileged=True), dict(command=['$HOME']), dict(command=['%h']),
                   dict(command=['x\nExecStart=/bin/sh']), dict(image='docker.io/library/busybox:latest'),
                   dict(image=self.spec['image'].replace('docker.io', 'untrusted.example')),
                   dict(ports=[{'bind': '0.0.0.0', 'host': 18100, 'container': 80}]),
                   dict(ports=[{'bind': '127.0.0.1', 'host': 18101, 'container': 80}]),
                   dict(limits={'memory_mib': 1024, 'cpu_millis': 1000}),
                   dict(volumes=[{'name': 'data', 'target': '/../etc', 'read_only': False}]),
                   dict(labels=[{'name': 'coolify.resource', 'value': 'foreign'}]),
                   dict(environment={'LD_PRELOAD': '/tmp/x'}), dict(network={'internal': False}),
                   dict(schema_version=True)]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                node.QuadletBundle.compile({**self.spec, **changes}, RESOURCE, self.policy)

    def test_policy_must_explicitly_grant_native_profile_and_limits(self):
        for key in self.policy:
            policy = copy.deepcopy(self.policy)
            del policy[key]
            if key == 'generation':
                continue
            with self.subTest(key=key), self.assertRaises(ValueError):
                node.QuadletBundle.compile(self.spec, RESOURCE, policy)

    def test_bundle_staging_is_verified_idempotent_and_does_not_publish(self):
        with tempfile.TemporaryDirectory() as directory:
            calls = []
            bundle = node.QuadletBundle(Path(directory), lambda path, files: calls.append(str(path)))
            first = bundle.stage(self.spec, RESOURCE, self.policy)
            second = bundle.stage(self.spec, RESOURCE, self.policy)
            self.assertEqual(first, second)
            self.assertEqual(first['state'], 'staged')
            self.assertEqual(len(calls), 1)
            self.assertFalse((Path(directory) / 'current').exists())
            release = Path(directory) / RESOURCE / first['bundle_hash']
            (release / f'coolify-{RESOURCE}.container').write_text('tampered')
            with self.assertRaises(ValueError):
                bundle.stage(self.spec, RESOURCE, self.policy)

    def test_rejected_generator_never_publishes_a_validated_bundle(self):
        def reject(path, files):
            raise ValueError('generator rejected candidate')
        with tempfile.TemporaryDirectory() as directory:
            bundle = node.QuadletBundle(Path(directory), reject)
            with self.assertRaises(ValueError):
                bundle.stage(self.spec, RESOURCE, self.policy)
            self.assertEqual(list((Path(directory) / RESOURCE).iterdir()), [])

    def test_shared_unicode_golden_preserves_php_spec_and_unit_bytes(self):
        fixtures = ROOT / 'tests/Fixtures/quadlet/prebuilt-v1-unicode'
        spec = json.loads((fixtures / 'spec.json').read_text())
        self.assertEqual(node.QuadletBundle.encoded(spec), (fixtures / 'spec.json').read_text().strip())
        files = node.QuadletBundle.compile(spec, RESOURCE, self.policy)
        for suffix, fixture in [('container', 'application.container'), ('network', 'application.network'), ('data.volume', 'data.volume')]:
            name = f'coolify-{RESOURCE}-data.volume' if suffix == 'data.volume' else f'coolify-{RESOURCE}.{suffix}'
            self.assertEqual(files[name], (fixtures / fixture).read_text())

    def test_existing_bundle_permissions_and_hard_links_are_rechecked(self):
        for changed in ['directory', 'file', 'hardlink']:
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as directory:
                bundle = node.QuadletBundle(Path(directory), lambda path, files: None)
                metadata = bundle.stage(self.spec, RESOURCE, self.policy)
                release = Path(directory) / RESOURCE / metadata['bundle_hash']
                target = release / f'coolify-{RESOURCE}.container'
                if changed == 'directory':
                    release.chmod(0o777)
                elif changed == 'file':
                    target.chmod(0o666)
                else:
                    os.link(target, Path(directory) / 'external-link')
                with self.assertRaises(ValueError):
                    bundle.stage(self.spec, RESOURCE, self.policy)


if __name__ == '__main__':
    unittest.main()
