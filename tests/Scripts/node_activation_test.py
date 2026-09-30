"""Durable activation contract; real systemd and interruption gates remain separate."""
import copy
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[2] / 'scripts/coolify-node-executor.py'
loader = importlib.util.spec_from_file_location('executor', SOURCE)
node = importlib.util.module_from_spec(loader)
loader.loader.exec_module(node)


class Native:
    def __init__(self):
        self.selected = None
        self.active = False
        self.invocation = ''
        self.effects = []
        self.healthy = True
        self.candidate_healthy = True
        self.image_ready = True
        self.image_failed = False
        self.fail_compensation = False
        self.failed = False
        self.failed_on_stop = False
        self.boot_selected = None
        self.pending_operation = None
        self.boot_changed = False
        self.candidate = 'a' * 64
        self.spec = {'image': 'registry.example/image@sha256:' + 'f' * 64,
                     'volumes': [], 'network': {'internal': True}, 'ports': [],
                     'migration_classification': 'none'}

    def load(self, resource_id, bundle_hash, policy):
        return {'metadata': {'bundle_hash': bundle_hash}, 'spec': copy.deepcopy(self.spec)}

    def current(self, resource_id, policy):
        return self.selected

    def preflight(self, resource_id, bundle_hash, policy):
        return self.inspect(resource_id, policy)

    def inspect(self, resource_id, policy, bundle_hash=None):
        if bundle_hash is not None and bundle_hash != self.selected:
            raise ValueError('Selected artifact mismatch')
        return {'active': self.active, 'invocation': self.invocation, 'transitioning': False,
                'failed': self.failed, 'bundle_hash': self.selected,
                'health': self.active and self.healthy and (self.selected != self.candidate or self.candidate_healthy) and not (self.fail_compensation and self.selected != self.candidate)}

    def image(self, resource_id, operation_id, spec, policy):
        return {'ready': self.image_ready, 'failed': self.image_failed}

    def stop(self, resource_id, policy):
        if self.active:
            self.effects.append('stop')
            self.active = False
            self.failed = self.failed_on_stop

    def begin_update(self, resource_id, operation_id, candidate, previous, policy):
        if self.pending_operation is None:
            self.pending_operation = operation_id
            self.boot_selected = None
        elif self.pending_operation != operation_id:
            raise ValueError('Different operation owns selection')

    def interrupted_update(self, resource_id, operation_id, policy):
        if self.pending_operation != operation_id:
            raise ValueError('Activation lacks pending selection')
        return self.selected is None and self.boot_changed

    def commit(self, resource_id, bundle_hash, operation_id, policy):
        if self.pending_operation not in {None, operation_id}:
            raise ValueError('Different operation owns commit')
        if self.boot_selected != bundle_hash:
            self.effects.append('commit:' + str(bundle_hash))
            self.boot_selected = bundle_hash
        self.pending_operation = None
        return self.inspect(resource_id, policy)

    def select(self, resource_id, bundle_hash, policy):
        if self.selected != bundle_hash:
            self.effects.append('select:' + str(bundle_hash))
            self.selected = bundle_hash

    def reload_verify(self, resource_id, bundle_hash, policy):
        self.effects.append('reload')

    def start(self, resource_id, policy, before_request=None):
        if before_request:
            before_request()
        if not self.active:
            self.effects.append('start')
            self.active = True
            self.failed = False
            self.invocation = uuid.uuid4().hex


class NodeActivationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.resource = str(uuid.uuid4())
        self.native = Native()
        runtime = type('Runtime', (), {'native': self.native})()
        self.executor = node.Executor(Path(self.temporary.name), runtime)
        self.addCleanup(self.executor.db.close)
        self.policy = {'version': 7, 'controller_epoch': 2, 'minimum_protocol': 2,
                       'execution_uid': 1001, 'execution_gid': 1001,
                       'principals': {'controller': {'resources': [self.resource], 'actions': ['activate', 'recover', 'events']}},
                       'resources': {self.resource: {'kind': 'quadlet', 'generation': 0, 'activation_timeout_seconds': 10}}}
        self.request = {'protocol': 2, 'execution_uid': 1001, 'execution_gid': 1001,
                        'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4()),
                        'resource_id': self.resource, 'action': 'activate', 'bundle_hash': self.native.candidate,
                        'expected_generation': 0, 'controller_epoch': 2, 'policy_version': 7,
                        'deadline': int(time.time()) + 60}

    def run_request(self, request=None):
        return self.executor.execute(request or self.request, 'controller', self.policy)

    def reconcile_until(self, predicate):
        for _ in range(20):
            result = self.run_request()
            if predicate(result):
                return result
            self.assertEqual(result['status'], 'executing', result)
        self.fail('Same operation did not converge within the bounded observation count')

    def terminal(self):
        return self.reconcile_until(lambda result: result['status'] != 'executing')

    def candidate_started(self):
        result = self.reconcile_until(lambda result: self.native.active and self.native.selected == self.native.candidate)
        self.assertEqual(result['status'], 'executing', result)

    def expire_activation(self):
        row = self.executor.db.execute('SELECT before_state FROM operations WHERE id=?', (self.request['operation_id'],)).fetchone()
        state = json.loads(row[0])
        state['activation_started_at'] = int(time.time()) - 1000
        with self.executor.db:
            self.executor.db.execute('UPDATE operations SET before_state=? WHERE id=?', (node.canonical(state), self.request['operation_id']))

    def compensation_started(self):
        result = self.reconcile_until(lambda outcome: self.native.active and self.native.selected == 'b' * 64)
        self.assertEqual(result['status'], 'executing', result)

    def test_activation_commits_and_duplicate_delivery_does_not_restart(self):
        first = self.terminal()
        self.assertEqual(first['status'], 'succeeded', first)
        self.assertEqual(first['generation'], 1)
        self.assertEqual(first['release']['state'], 'committed')
        self.assertFalse(first['release']['no_op'])
        effects = list(self.native.effects)
        self.assertEqual(self.run_request(), first)
        self.assertEqual(self.native.effects, effects)
        self.assertEqual(self.native.effects.count('start'), 1)

    def test_fresh_identical_healthy_intent_is_a_no_op(self):
        self.native.selected = self.native.candidate
        self.native.active = True
        self.native.invocation = 'existing'
        result = self.terminal()
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertTrue(result['release']['no_op'])
        self.assertEqual(result['generation'], 0)
        self.assertEqual(self.native.effects, [])

    def test_crash_after_selection_resumes_same_intent_and_starts_once(self):
        original = self.native.select
        def killed(*arguments, **keywords):
            original(*arguments, **keywords)
            raise KeyboardInterrupt('after selector publication, before reload')
        self.native.select = killed
        with self.assertRaises(KeyboardInterrupt):
            self.run_request()
        self.native.select = original
        result = self.terminal()
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(self.native.effects.count('start'), 1)
        self.assertEqual(self.native.effects.count('select:' + self.native.candidate), 1)

    def test_crash_after_start_observes_success_without_second_start(self):
        original = self.native.start
        def killed(*arguments, **keywords):
            original(*arguments, **keywords)
            raise KeyboardInterrupt('lost reply after start')
        self.native.start = killed
        with self.assertRaises(KeyboardInterrupt):
            self.run_request()
        self.native.start = original
        result = self.terminal()
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(self.native.effects.count('start'), 1)

    def test_running_image_acquisition_keeps_previous_writer_and_reconciles(self):
        self.native.selected = 'b' * 64
        self.native.active = True
        self.native.image_ready = False
        result = self.run_request()
        self.assertEqual(result['status'], 'executing', result)
        self.assertEqual(self.native.effects, [])
        self.native.image_ready = True
        self.assertEqual(self.terminal()['status'], 'succeeded')
        self.assertEqual(self.native.effects.count('stop'), 1)

    def test_failed_candidate_compensates_application_and_reports_data_unchanged(self):
        previous = 'b' * 64
        self.native.selected = previous
        self.native.active = True
        self.native.candidate_healthy = False
        self.candidate_started()
        self.expire_activation()
        result = self.terminal()
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['release']['state'], 'rolled_back')
        self.assertIs(result['release']['data_recovery'], False)
        self.assertEqual(result['generation'], 1)
        self.assertEqual(self.native.selected, previous)
        self.assertTrue(self.native.active)
        self.assertEqual(self.run_request(), result)

    def test_unsafe_data_migration_never_starts_previous_application(self):
        self.native.selected = 'b' * 64
        self.native.active = True
        self.native.candidate_healthy = False
        self.native.spec['migration_classification'] = 'forward-only'
        self.candidate_started()
        self.expire_activation()
        result = self.terminal()
        self.assertEqual(result['status'], 'needs_intervention', result)
        self.assertFalse(self.native.active)
        self.assertIsNone(self.native.selected)
        self.assertEqual(self.native.effects.count('start'), 1)

    def test_failed_image_does_not_stop_previous_writer_or_repeat_pull(self):
        self.native.selected = 'b' * 64
        self.native.active = True
        self.native.image_failed = True
        result = self.terminal()
        self.assertEqual(result['status'], 'denied')
        self.assertEqual(result['code'], 'image_acquisition_failed')
        self.assertEqual(self.native.effects, [])
        self.native.image_failed = False
        self.assertEqual(self.run_request(), result)
        self.assertEqual(self.native.effects, [])

    def test_stale_generation_and_changed_definition_cannot_start_candidate(self):
        self.request['expected_generation'] = 99
        self.assertEqual(self.run_request()['code'], 'stale_generation')
        self.assertEqual(self.native.effects, [])
        self.request['expected_generation'] = 0
        self.native.image_ready = False
        self.assertEqual(self.run_request()['status'], 'executing')
        self.policy['resources'][self.resource]['activation_timeout_seconds'] = 20
        self.native.image_ready = True
        self.assertEqual(self.run_request()['code'], 'recovery_definition_mismatch')
        self.assertEqual(self.native.effects, [])

    def test_compensation_failure_is_not_a_success_or_data_recovery(self):
        self.native.selected = 'b' * 64
        self.native.active = True
        self.native.candidate_healthy = False
        self.candidate_started()
        self.expire_activation()
        self.native.fail_compensation = True
        self.reconcile_until(lambda result: self.native.selected != self.native.candidate and self.native.active)
        row = self.executor.db.execute('SELECT before_state FROM operations WHERE id=?', (self.request['operation_id'],)).fetchone()
        before = json.loads(row[0])
        before['compensation_activation_started_at'] = int(time.time()) - 1000
        with self.executor.db:
            self.executor.db.execute('UPDATE operations SET before_state=? WHERE id=?', (node.canonical(before), self.request['operation_id']))
        result = self.terminal()
        self.assertEqual(result['status'], 'needs_intervention')
        self.assertEqual(result['code'], 'compensation_failed')
        self.assertNotIn('data_recovery', result)

    def test_previous_health_budget_starts_after_compensating_publication(self):
        self.native.selected = 'b' * 64
        self.native.active = True
        self.native.candidate_healthy = False
        self.candidate_started()
        self.expire_activation()
        self.assertEqual(self.run_request()['status'], 'executing')
        row = self.executor.db.execute('SELECT before_state FROM operations WHERE id=?', (self.request['operation_id'],)).fetchone()
        before = json.loads(row[0])
        self.assertEqual(before['phase'], 'compensating_stop')
        before['compensation_started_at'] = int(time.time()) - 1000
        with self.executor.db:
            self.executor.db.execute('UPDATE operations SET before_state=? WHERE id=?', (node.canonical(before), self.request['operation_id']))
        result = self.terminal()
        self.assertEqual(result['status'], 'failed', result)
        self.assertTrue(self.native.active)
        self.assertEqual(self.native.effects.count('start'), 2)

    def test_expected_failed_stop_state_does_not_prevent_first_candidate_or_restore_start(self):
        self.native.selected = 'b' * 64
        self.native.active = True
        self.native.failed_on_stop = True
        self.native.candidate_healthy = False
        self.candidate_started()
        self.expire_activation()
        result = self.terminal()
        self.assertEqual(result['status'], 'failed', result)
        self.assertTrue(result['observed']['health'])
        self.assertEqual(self.native.effects.count('start'), 2)

    def test_validation_time_does_not_expire_health_budget_before_first_start(self):
        clock = [time.time()]
        inspect = self.native.inspect
        def slow_inspect(*args, **kwargs):
            clock[0] += 6
            return inspect(*args, **kwargs)
        self.native.inspect = slow_inspect
        with patch.object(node.time, 'time', side_effect=lambda: clock[0]):
            result = self.terminal()
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(self.native.effects.count('start'), 1)

    def test_failed_adapter_observation_emits_bounded_context_without_exception_text(self):
        self.native.load = lambda *args: (_ for _ in ()).throw(ValueError('credential-secret'))
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            result = self.run_request()
        # Admission rejects before journaling; exercise a journaled observation failure.
        self.assertEqual(result['status'], 'denied')
        self.native.load = lambda *args: {'metadata': {}, 'spec': copy.deepcopy(self.native.spec)}
        self.candidate_started()
        self.native.inspect = lambda *args: (_ for _ in ()).throw(ValueError('credential-secret'))
        with contextlib.redirect_stderr(errors):
            result = self.run_request()
        self.assertEqual(result['status'], 'needs_intervention')
        diagnostic = json.loads(errors.getvalue())
        self.assertEqual(diagnostic['operation_id'], self.request['operation_id'])
        self.assertEqual(diagnostic['phase'], 'activating')
        self.assertEqual(diagnostic['exception'], 'ValueError')
        self.assertNotIn('credential-secret', errors.getvalue())
        self.assertLess(len(errors.getvalue()), 512)

    def test_start_preflight_time_is_excluded_from_candidate_and_compensation_budget(self):
        self.native.selected = 'b' * 64
        self.native.active = True
        self.native.candidate_healthy = False
        clock = [int(time.time())]
        start = self.native.start
        def slow_start(resource_id, policy, before_request=None):
            clock[0] += 30
            start(resource_id, policy, before_request)
        self.native.start = slow_start
        with patch.object(node.time, 'time', side_effect=lambda: clock[0]):
            self.candidate_started()
            row = self.executor.db.execute('SELECT before_state FROM operations WHERE id=?', (self.request['operation_id'],)).fetchone()
            self.assertEqual(json.loads(row[0])['activation_started_at'], clock[0])
            self.expire_activation()
            self.compensation_started()
            row = self.executor.db.execute('SELECT before_state FROM operations WHERE id=?', (self.request['operation_id'],)).fetchone()
            self.assertEqual(json.loads(row[0])['compensation_activation_started_at'], clock[0])
            self.assertEqual(self.terminal()['status'], 'failed')

    def test_second_boot_during_compensation_reselects_prior_release_with_fresh_budget(self):
        self.native.selected = 'b' * 64
        self.native.active = True
        self.native.candidate_healthy = False
        self.candidate_started()
        self.expire_activation()
        self.compensation_started()
        self.native.boot_changed = True
        self.native.selected = None
        self.native.active = False
        self.native.invocation = ''
        result = self.run_request()
        self.assertEqual(result['status'], 'executing', result)
        self.assertEqual(self.native.selected, 'b' * 64)
        self.assertTrue(self.native.active)
        self.assertEqual(self.terminal()['status'], 'failed')

    def test_second_boot_during_accepted_restart_preserves_accepted_candidate(self):
        self.run_request()
        commit = self.native.commit
        self.native.commit = lambda *args: (_ for _ in ()).throw(KeyboardInterrupt())
        with self.assertRaises(KeyboardInterrupt):
            self.run_request()
        self.native.commit = commit
        for _ in range(2):
            self.native.boot_changed = True
            self.native.selected = None
            self.native.active = False
            self.native.invocation = ''
            result = self.run_request()
            self.assertEqual(result['status'], 'executing', result)
            self.assertEqual(self.native.selected, self.native.candidate)
            self.assertTrue(self.native.active)
        self.assertEqual(self.terminal()['status'], 'succeeded')
        self.assertEqual(self.native.effects.count('start'), 3)

    def test_only_health_accepted_release_gets_boot_selection(self):
        self.native.candidate_healthy = False
        self.candidate_started()
        self.assertIsNone(self.native.boot_selected)
        self.native.candidate_healthy = True
        result = self.terminal()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(self.native.boot_selected, self.native.candidate)

    def test_boot_interruption_safely_compensates_same_operation(self):
        self.native.selected = 'b' * 64
        self.native.boot_selected = 'b' * 64
        self.native.active = True
        self.native.candidate_healthy = False
        self.candidate_started()
        self.assertIsNone(self.native.boot_selected)
        self.native.boot_changed = True
        self.native.selected = None
        self.native.active = False
        result = self.terminal()
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(self.native.boot_selected, 'b' * 64)
        self.assertEqual(result['release']['data_recovery'], False)

    def test_boot_interruption_after_forward_migration_never_boots_previous_writer(self):
        self.native.selected = 'b' * 64
        self.native.boot_selected = 'b' * 64
        self.native.active = True
        self.native.candidate_healthy = False
        self.native.spec['migration_classification'] = 'forward-only'
        self.candidate_started()
        self.native.boot_changed = True
        self.native.selected = None
        self.native.active = False
        result = self.terminal()
        self.assertEqual(result['status'], 'needs_intervention', result)
        self.assertEqual(result['code'], 'unsafe_data_rollback')
        self.assertIsNone(self.native.boot_selected)
        self.assertFalse(self.native.active)

    def test_crash_after_boot_publication_finishes_once_without_another_start(self):
        self.run_request()
        commit = self.native.commit
        def killed(*args):
            commit(*args)
            raise KeyboardInterrupt('after boot pointer publication, before terminal journal')
        self.native.commit = killed
        with self.assertRaises(KeyboardInterrupt):
            self.run_request()
        self.native.commit = commit
        result = self.terminal()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['generation'], 1)
        self.assertEqual(self.native.effects.count('start'), 1)
        self.assertEqual(self.native.effects.count('commit:' + self.native.candidate), 1)

    def test_boot_after_health_acceptance_before_boot_publication_restarts_accepted_release(self):
        self.run_request()
        commit = self.native.commit
        def killed(*args):
            raise KeyboardInterrupt('after journal commit checkpoint, before boot pointer')
        self.native.commit = killed
        with self.assertRaises(KeyboardInterrupt):
            self.run_request()
        self.native.commit = commit
        self.native.boot_changed = True
        self.native.selected = None
        self.native.active = False
        result = self.terminal()
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(self.native.boot_selected, self.native.candidate)
        self.assertEqual(result['generation'], 1)
        self.assertEqual(self.native.effects.count('start'), 2)

    def test_conflicting_or_unscoped_bundle_is_denied_before_effects(self):
        for changed in [{'bundle_hash': '../other'}, {'resource_id': str(uuid.uuid4())}, {'unit': 'arbitrary.service'}]:
            result = self.run_request({**self.request, **changed})
            self.assertEqual(result['status'], 'denied', result)
        self.assertEqual(self.native.effects, [])


if __name__ == '__main__':
    unittest.main()
