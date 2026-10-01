"""Native publication input/ownership gates; live systemd gates are separate."""
import copy
import importlib.util
import json
import os
from pathlib import Path
import socket
import tempfile
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]

def module(name, path):
    loader = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(result)
    return result

node = module('executor', ROOT / 'scripts/coolify-node-executor.py')
native = module('native', ROOT / 'scripts/coolify-node-native.py')

class NativePublicationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.resource = str(uuid.uuid4())
        self.policy = {'kind': 'quadlet', 'profile': 'prebuilt-v1', 'host_ports': [18100],
                       'image_registries': ['docker.io'], 'max_memory_mib': 256,
                       'max_cpu_millis': 1000, 'max_volumes': 2}
        self.spec = json.loads((ROOT / 'tests/Fixtures/quadlet/prebuilt-v1/spec.json').read_text())
        self.runtime = native.NativeRuntime(os.getuid(), self.root, node.QuadletBundle)
        self.runtime.home = self.root / 'home'
        self.runtime.home.mkdir(mode=0o700)
        self.runtime.runtime_directory = self.root / 'runtime'
        self.runtime.runtime_directory.mkdir(mode=0o700)
        self.runtime.boot_id = lambda: '11111111-1111-4111-8111-111111111111'
        self.runtime.check_versions = lambda policy: None
        bundle = node.QuadletBundle(self.root / 'releases', lambda *args: None)
        self.metadata = bundle.stage(self.spec, self.resource, self.policy)
        self.digest = self.metadata['bundle_hash']

    def test_load_validates_exact_bundle_and_never_creates_missing_release(self):
        loaded = self.runtime.load(self.resource, self.digest, self.policy)
        self.assertEqual(loaded['spec'], self.spec)
        with self.assertRaises(ValueError):
            self.runtime.load(self.resource, 'f' * 64, self.policy)
        self.assertFalse((self.root / 'releases' / self.resource / ('f' * 64)).exists())

    def test_tampered_and_world_readable_bundle_are_rejected(self):
        manifest = self.root / 'releases' / self.resource / self.digest / 'manifest.json'
        manifest.chmod(0o644)
        with self.assertRaises(ValueError):
            self.runtime.load(self.resource, self.digest, self.policy)
        manifest.chmod(0o600)
        manifest.write_text('{}\n')
        with self.assertRaises(ValueError):
            self.runtime.load(self.resource, self.digest, self.policy)

    def test_selection_is_atomic_idempotent_and_scoped(self):
        self.assertIsNone(self.runtime.current(self.resource, self.policy))
        self.runtime.select(self.resource, self.digest, self.policy)
        selector = self.runtime.selection(self.resource)
        inode = selector.lstat().st_ino
        self.assertEqual(self.runtime.current(self.resource, self.policy), self.digest)
        self.runtime.select(self.resource, self.digest, self.policy)
        self.assertEqual(selector.lstat().st_ino, inode)
        self.runtime.select(self.resource, None, self.policy)
        self.assertFalse(selector.is_symlink())
        self.assertTrue((self.root / 'releases' / self.resource / self.digest).is_dir())

    def test_foreign_selector_directory_or_target_is_never_replaced(self):
        self.runtime.select(self.resource, self.digest, self.policy)
        selector = self.runtime.selection(self.resource)
        selector.unlink()
        selector.symlink_to(self.root)
        with self.assertRaises(ValueError):
            self.runtime.select(self.resource, None, self.policy)
        self.assertTrue(selector.is_symlink())
        selector.unlink()
        selector.mkdir()
        with self.assertRaises(ValueError):
            self.runtime.select(self.resource, self.digest, self.policy)
        self.assertTrue(selector.is_dir())

    def test_unscoped_resource_and_bundle_path_are_rejected(self):
        for resource, digest in [('..', self.digest), (self.resource, '../data')]:
            with self.assertRaises(ValueError):
                self.runtime.load(resource, digest, self.policy)

    def test_foreign_runtime_labels_and_second_owner_are_rejected(self):
        valid = {'coolify.managed': 'true', 'coolify.resource': self.resource}
        self.runtime.owned(valid, self.resource)
        for labels in [{}, {**valid, 'coolify.resource': str(uuid.uuid4())},
                       {**valid, 'io.containers.autoupdate': 'registry'},
                       {**valid, 'com.docker.compose.project': 'elsewhere'}]:
            with self.assertRaises(ValueError):
                self.runtime.owned(labels, self.resource)

    def test_global_prefix_and_matching_unit_dropins_are_rejected(self):
        search = self.root / 'search'
        search.mkdir()
        self.runtime.quadlet_roots = lambda: [search]
        for name in ['container.d', 'coolify-.container.d', f'coolify-{self.resource}.container.d']:
            override = search / name
            override.mkdir()
            (override / 'unsafe.conf').write_text('[Container]\nPodmanArgs=--privileged\n')
            with self.assertRaises(ValueError):
                self.runtime.check_quadlet_sources(self.resource, [f'coolify-{self.resource}.container'])
            (override / 'unsafe.conf').unlink()
            override.rmdir()
        self.runtime.check_quadlet_sources(self.resource, [f'coolify-{self.resource}.container'])

    def test_other_resource_sources_are_preserved_but_matching_foreign_source_denied(self):
        search = self.root / 'search'
        search.mkdir()
        self.runtime.quadlet_roots = lambda: [search]
        unrelated = search / 'coolify-unrelated.container'
        unrelated.write_text('Unrelated\n')
        self.runtime.check_quadlet_sources(self.resource, [f'coolify-{self.resource}.container'])
        foreign = search / f'coolify-{self.resource}.container'
        foreign.write_text('Foreign\n')
        with self.assertRaises(ValueError):
            self.runtime.check_quadlet_sources(self.resource, [foreign.name])
        self.assertEqual(unrelated.read_text(), 'Unrelated\n')
        self.assertEqual(foreign.read_text(), 'Foreign\n')

    def test_pending_selection_cannot_boot_candidate_or_previous_until_acceptance(self):
        operation = str(uuid.uuid4())
        self.runtime.select(self.resource, self.digest, self.policy)
        self.runtime.begin_update(self.resource, operation, self.digest, self.digest, self.policy)
        self.assertFalse(self.runtime.selection(self.resource).is_symlink())
        self.runtime.select(self.resource, self.digest, self.policy)
        volatile = self.runtime.active_selection(self.resource)
        self.assertNotEqual(volatile, self.runtime.selection(self.resource))
        self.assertTrue(volatile.is_symlink())
        self.assertEqual(self.runtime.current(self.resource, self.policy), self.digest)
        volatile.unlink()
        self.runtime.boot_id = lambda: '22222222-2222-4222-8222-222222222222'
        self.assertIsNone(self.runtime.current(self.resource, self.policy))
        self.assertTrue(self.runtime.interrupted_update(self.resource, operation, self.policy))
        self.runtime.select(self.resource, self.digest, self.policy)
        self.runtime.reload_verify = lambda *args: None
        self.runtime.inspect = lambda *args: {'active': True, 'health': True, 'bundle_hash': self.digest}
        self.runtime.commit(self.resource, self.digest, operation, self.policy)
        self.assertEqual(self.runtime.active_selection(self.resource), self.runtime.selection(self.resource))
        self.assertTrue(self.runtime.selection(self.resource).is_symlink())
        self.assertIsNone(self.runtime.pending(self.resource))

    def test_pending_marker_cannot_change_operation_or_allow_foreign_commit(self):
        operation = str(uuid.uuid4())
        self.runtime.begin_update(self.resource, operation, self.digest, None, self.policy)
        with self.assertRaises(ValueError):
            self.runtime.begin_update(self.resource, str(uuid.uuid4()), self.digest, None, self.policy)
        self.runtime.select(self.resource, self.digest, self.policy)
        with self.assertRaises(ValueError):
            self.runtime.commit(self.resource, self.digest, str(uuid.uuid4()), self.policy)
        self.assertFalse(self.runtime.selection(self.resource).is_symlink())

    def test_unhealthy_candidate_cannot_be_published_for_boot(self):
        operation = str(uuid.uuid4())
        self.runtime.begin_update(self.resource, operation, self.digest, None, self.policy)
        self.runtime.select(self.resource, self.digest, self.policy)
        self.runtime.inspect = lambda *args: {'active': True, 'health': False, 'bundle_hash': self.digest}
        with self.assertRaises(ValueError):
            self.runtime.commit(self.resource, self.digest, operation, self.policy)
        self.assertFalse(self.runtime.selection(self.resource).is_symlink())

    def test_manual_stop_mask_is_owned_durable_and_preserves_the_selected_bundle(self):
        self.runtime.select(self.resource, self.digest, self.policy)
        operation = str(uuid.uuid4())
        self.runtime.set_stopped(self.resource, self.digest, operation, self.policy)
        mask = self.runtime.stop_mask(self.resource)
        self.assertTrue(mask.is_symlink())
        self.assertEqual(os.readlink(mask), '/dev/null')
        self.assertEqual(self.runtime.stopped(self.resource)['operation_id'], operation)
        self.assertEqual(self.runtime.current(self.resource, self.policy), self.digest)
        inode = mask.lstat().st_ino
        self.runtime.set_stopped(self.resource, self.digest, operation, self.policy)
        self.assertEqual(mask.lstat().st_ino, inode)
        self.runtime.clear_stopped(self.resource)
        self.assertFalse(mask.is_symlink())
        self.assertIsNone(self.runtime.stopped(self.resource))
        self.assertEqual(self.runtime.current(self.resource, self.policy), self.digest)

    def test_stop_persists_mask_without_reloading_away_the_running_quadlet_execstop(self):
        self.runtime.select(self.resource, self.digest, self.policy)
        self.runtime.check_quadlet_sources = lambda *args: None
        self.runtime.check_systemd_sources = lambda *args: None
        self.runtime.preflight = lambda *args: None
        reloads = []
        self.runtime.reload_verify = lambda *args: reloads.append(args)
        self.runtime.prepare_lifecycle('stop', self.resource, self.digest, str(uuid.uuid4()), self.policy)
        self.assertTrue(self.runtime.owns_stop_mask(self.resource))
        self.assertEqual(reloads, [], 'Reloading a masked active unit removes its Quadlet ExecStop')

    def test_foreign_mask_and_unprotected_stop_record_are_never_adopted(self):
        self.runtime.select(self.resource, self.digest, self.policy)
        mask = self.runtime.stop_mask(self.resource)
        mask.parent.mkdir(parents=True, mode=0o700)
        mask.parent.parent.chmod(0o700)
        mask.symlink_to('/dev/null')
        with self.assertRaises(ValueError):
            self.runtime.set_stopped(self.resource, self.digest, str(uuid.uuid4()), self.policy)
        with self.assertRaises(ValueError):
            self.runtime.clear_stopped(self.resource)
        self.assertTrue(mask.is_symlink())
        mask.unlink()
        self.runtime.set_stopped(self.resource, self.digest, str(uuid.uuid4()), self.policy)
        self.runtime.stopped_path(self.resource).chmod(0o644)
        with self.assertRaises(ValueError):
            self.runtime.clear_stopped(self.resource)
        self.assertTrue(mask.is_symlink())

    def test_stop_preparation_recovers_missing_mask_but_never_replaces_foreign_content(self):
        self.runtime.select(self.resource, self.digest, self.policy)
        operation = str(uuid.uuid4())
        self.runtime.set_stopped(self.resource, self.digest, operation, self.policy)
        mask = self.runtime.stop_mask(self.resource)
        mask.unlink()
        self.runtime.set_stopped(self.resource, self.digest, operation, self.policy)
        self.assertEqual(os.readlink(mask), '/dev/null')
        mask.unlink()
        mask.write_text('unrelated unit')
        with self.assertRaises(ValueError):
            self.runtime.set_stopped(self.resource, self.digest, operation, self.policy)
        self.assertEqual(mask.read_text(), 'unrelated unit')

    def test_closed_backend_time_wait_does_not_block_recreate_but_live_listener_does(self):
        with socket.socket() as server, socket.socket() as client:
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind(('127.0.0.1', 0))
            port = server.getsockname()[1]
            server.listen()
            client.connect(('127.0.0.1', port))
            connection, _ = server.accept()
            connection.shutdown(socket.SHUT_WR)
            self.assertEqual(client.recv(1), b'')
            client.close()
            connection.close()
            server.close()
        # The backend port remains in TIME_WAIT despite no surviving listener.
        with socket.socket() as probe:
            with self.assertRaises(OSError):
                probe.bind(('127.0.0.1', port))
        self.runtime.check_listener_available(port)
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', port))
            listener.listen()
            with self.assertRaises(OSError):
                self.runtime.check_listener_available(port)

    def test_start_records_intent_after_validation_and_before_the_manager_request(self):
        events = []
        self.runtime.inspect = lambda *args: {'bundle_hash': self.digest}
        self.runtime.preflight = lambda *args: events.append('validated')
        self.runtime.run = lambda arguments: events.append(arguments)
        self.runtime.start(self.resource, self.policy, before_request=lambda: events.append('durable-intent'))
        self.assertEqual(events, ['validated', 'durable-intent',
            ['/usr/bin/systemctl', '--user', '--no-block', 'start', 'coolify-' + self.resource + '.service']])
        events.clear()
        self.runtime.preflight = lambda *args: (_ for _ in ()).throw(ValueError('Rejected candidate'))
        with self.assertRaises(ValueError):
            self.runtime.start(self.resource, self.policy, before_request=lambda: events.append('durable-intent'))
        self.assertEqual(events, [])

    def test_inspection_refreshes_manager_state_after_object_probes_without_mixing_invocations(self):
        self.runtime.current = lambda *args: self.digest
        self.runtime.load = lambda *args: {'spec': self.spec}
        self.runtime.verify_effective = lambda *args: None
        self.runtime.check_objects = lambda *args: {'State': {'Running': True}}
        states = [dict(ActiveState='activating', InvocationID='same'), dict(ActiveState='active', InvocationID='same')]
        self.runtime.show = lambda *args: states.pop(0)
        with patch.object(native.http.client, 'HTTPConnection') as connection:
            connection.return_value.getresponse.return_value.status = 200
            result = self.runtime.inspect(self.resource, self.policy)
            self.assertTrue(result['active'])
            self.assertTrue(result['health'])
            self.assertFalse(result['transitioning'])
        states.extend([dict(ActiveState='active', InvocationID='old'), dict(ActiveState='active', InvocationID='new')])
        with patch.object(native.http.client, 'HTTPConnection') as connection:
            result = self.runtime.inspect(self.resource, self.policy)
            self.assertFalse(result['active'])
            self.assertFalse(result['health'])
            self.assertTrue(result['transitioning'])
            self.assertEqual(result['invocation'], 'new')
            connection.assert_not_called()

if __name__ == '__main__':
    unittest.main()
