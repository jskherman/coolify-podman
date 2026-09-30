import importlib.machinery
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


loader = importlib.machinery.SourceFileLoader('runtime_vm', str(Path(__file__).resolve().parents[2] / 'scripts/runtime-test-vm'))
spec = importlib.util.spec_from_loader(loader.name, loader)
vm = importlib.util.module_from_spec(spec)
loader.exec_module(vm)


class RuntimeVmRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.state = Path(self.directory.name)
        self.record = {'operation_id': 'fixture-existing', 'stage': 'running'}
        (self.state / 'state.json').write_text(json.dumps(self.record))
        patcher = patch.object(vm, 'STATE', self.state)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_monitor_identity_prevents_duplicate_launch_even_when_pid_is_invisible(self):
        (self.state / 'qmp.sock').touch()
        with patch.object(vm, 'monitor', return_value={'name': 'fixture-existing'}), patch.object(vm, 'run') as run:
            vm.start(self.record)
            run.assert_not_called()

    def test_denied_observation_does_not_prepare_or_launch(self):
        (self.state / 'qmp.sock').touch()
        before = (self.state / 'state.json').read_bytes()
        with patch.object(vm, 'monitor', side_effect=PermissionError('sandbox')), patch.object(vm, 'run') as run:
            with self.assertRaises(PermissionError):
                vm.start(self.record)
            run.assert_not_called()
        self.assertEqual((self.state / 'state.json').read_bytes(), before)

    def test_different_monitor_identity_is_rejected(self):
        (self.state / 'qmp.sock').touch()
        with patch.object(vm, 'monitor', return_value={'name': 'unrelated'}):
            with self.assertRaisesRegex(RuntimeError, 'identity mismatch'):
                vm.running()

    def test_missing_process_is_not_assumed_to_be_a_safe_new_launch(self):
        with self.assertRaisesRegex(RuntimeError, 'Uncertain fixture state'):
            vm.running()

    def test_explicitly_stopped_fixture_can_start(self):
        vm.save({**self.record, 'stage': 'stopped'})
        self.assertFalse(vm.running())


if __name__ == '__main__':
    unittest.main()
