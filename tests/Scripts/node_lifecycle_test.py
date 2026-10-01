"""Native lifecycle intent, duplicate delivery and interruption regression gates."""
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from node_activation_test import Native, node


class NativeLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.native = Native()
        self.native.selected = 'b' * 64
        self.native.active = True
        self.native.invocation = uuid.uuid4().hex
        self.resource = str(uuid.uuid4())
        runtime = type('Runtime', (), {'native': self.native, 'inspect': lambda _, resource: self.native.inspect(self.resource, resource)})()
        self.executor = node.Executor(Path(self.temporary.name), runtime)
        self.addCleanup(self.executor.db.close)
        self.policy = {'version': 10, 'controller_epoch': 2, 'minimum_protocol': 2,
                       'execution_uid': 1001, 'execution_gid': 1001,
                       'principals': {'controller': {'resources': [self.resource], 'actions': ['start', 'stop', 'restart', 'status', 'recover']}},
                       'resources': {self.resource: {'kind': 'quadlet', 'generation': 0}}}

    def request(self, action, generation=0):
        return {'protocol': 2, 'execution_uid': 1001, 'execution_gid': 1001,
                'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4()),
                'resource_id': self.resource, 'action': action,
                'expected_generation': generation, 'controller_epoch': 2, 'policy_version': 10,
                'deadline': int(time.time()) + 60}

    def run_request(self, request):
        return self.executor.execute(request, 'controller', self.policy)

    def test_stop_disables_boot_even_when_already_inactive_and_replay_has_no_effect(self):
        self.native.active = False
        request = self.request('stop')
        result = self.run_request(request)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['generation'], 1)
        self.assertFalse(self.native.boot_enabled)
        self.assertFalse(result['observed']['boot_enabled'])
        effects = list(self.native.effects)
        self.assertEqual(self.run_request(request), result)
        self.assertEqual(self.native.effects, effects)
        result = self.run_request(self.request('stop', 1))
        self.assertEqual(result['generation'], 1)
        self.assertEqual(self.native.effects, effects)

    def test_start_after_stop_restores_boot_and_restart_uses_the_owner_once(self):
        self.assertEqual(self.run_request(self.request('stop'))['generation'], 1)
        self.assertFalse(self.native.active)
        self.assertEqual(self.run_request(self.request('start', 1))['generation'], 2)
        self.assertTrue(self.native.boot_enabled)
        self.assertTrue(self.native.active)
        invocation = self.native.invocation
        request = self.request('restart', 2)
        result = self.run_request(request)
        self.assertEqual(result['generation'], 3)
        self.assertNotEqual(self.native.invocation, invocation)
        self.run_request(request)
        self.assertEqual(self.native.effects.count('request:restart'), 1)

    def test_lost_reply_reconciles_the_original_stop_without_requesting_again(self):
        apply = self.native.request_lifecycle
        def interrupted(action, resource):
            apply(action, resource)
            raise SystemExit('lost executor after actual systemd request')
        self.native.request_lifecycle = interrupted
        request = self.request('stop')
        with self.assertRaises(SystemExit):
            self.run_request(request)
        result = self.run_request(request)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['generation'], 1)
        self.assertEqual(self.native.effects.count('request:stop'), 1)

    def test_preparation_interruption_resumes_but_uncertain_command_is_not_replayed(self):
        self.native.active = False
        prepare = self.native.prepare_lifecycle
        def interrupted(*args):
            prepare(*args)
            raise SystemExit('lost executor after boot intent persisted')
        self.native.prepare_lifecycle = interrupted
        request = self.request('stop')
        with self.assertRaises(SystemExit):
            self.run_request(request)
        self.native.prepare_lifecycle = prepare
        result = self.run_request(request)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['generation'], 1)
        self.assertEqual(self.native.effects.count('request:stop'), 1)
        self.native.active = False
        self.native.request_lifecycle = lambda *args: (_ for _ in ()).throw(TimeoutError())
        start = self.request('start', 1)
        self.assertEqual(self.run_request(start)['status'], 'needs_intervention')
        self.native.request_lifecycle = lambda *args: self.fail('An uncertain command must not be repeated')
        self.assertEqual(self.run_request(start)['status'], 'needs_intervention')

    def test_transitioning_stop_cannot_be_reported_as_success(self):
        inspect = self.native.inspect
        self.native.inspect = lambda *args: {**inspect(*args), 'transitioning': True}
        result = self.run_request(self.request('stop'))
        self.assertEqual(result['status'], 'needs_intervention')


if __name__ == '__main__':
    unittest.main()
