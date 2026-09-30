"""Restricted node operation contract. Runtime gates additionally require systemd/SSH."""
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid

MODULE = Path(__file__).resolve().parents[2] / 'scripts/coolify-node-executor.py'


class NodeExecutorTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('node_executor', MODULE)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.resource = str(uuid.uuid4())
        self.policy = {
            'version': 1, 'controller_epoch': 7,
            'principals': {'controller-a': {'resources': [self.resource], 'actions': ['status', 'start', 'stop', 'restart']}},
            'resources': {self.resource: {'unit': 'coolify-fixture.service', 'generation': 1}},
        }
        self.effects = []
        self.observed = {'active': True, 'invocation': 'first'}

        class Runtime:
            def inspect(inner, resource):
                return dict(self.observed)

            def apply(inner, action, resource):
                self.effects.append(action)
                self.observed = {'active': action != 'stop', 'invocation': str(uuid.uuid4()) if action != 'stop' else ''}

        self.executor = self.module.Executor(Path(self.directory.name), Runtime())
        self.addCleanup(self.executor.db.close)
        self.request = {
            'protocol': 1, 'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4()),
            'resource_id': self.resource, 'action': 'stop', 'expected_generation': 1,
            'controller_epoch': 7, 'policy_version': 1, 'deadline': int(time.time()) + 60,
        }

    def execute(self, request=None, principal='controller-a'):
        return self.executor.execute(request or self.request, principal, self.policy)

    def test_duplicate_delivery_returns_durable_outcome_without_second_effect(self):
        first = self.execute()
        second = self.execute()
        self.assertEqual(first, second)
        self.assertEqual(first['generation'], 2)
        self.assertEqual(self.effects, ['stop'])

    def test_recovery_chains_are_bounded_and_cannot_replay_original_effect(self):
        original = self.execute()
        self.policy['principals']['controller-a']['actions'].append('recover')
        target = original['operation_id']
        for _ in range(8):
            request = {**self.request, 'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4()),
                       'action': 'recover', 'target_operation_id': target, 'expected_generation': 2}
            outcome = self.execute(request)
            self.assertEqual(outcome['status'], 'succeeded', outcome)
            target = request['operation_id']
        request = {**request, 'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4()), 'target_operation_id': target}
        rejected = self.execute(request)
        self.assertEqual(rejected['code'], 'recovery_chain_too_deep')
        self.assertEqual(self.effects, ['stop'])

    def prepare_stage(self):
        self.request.update(protocol=2, execution_uid=1001, execution_gid=1001, action='stage', expected_generation=0,
                            spec=json.loads((MODULE.parent.parent / 'tests/Fixtures/quadlet/prebuilt-v1/spec.json').read_text()))
        self.policy.update(execution_uid=1001, execution_gid=1001)
        self.policy['principals']['controller-a']['actions'].append('stage')
        self.policy['resources'][self.resource] = {
            'kind': 'quadlet', 'profile': 'prebuilt-v1', 'generation': 0,
            'host_ports': [18100], 'image_registries': ['docker.io'],
            'max_memory_mib': 256, 'max_cpu_millis': 1000, 'max_volumes': 2,
        }
        self.bundles = self.module.QuadletBundle(Path(self.directory.name) / 'releases', lambda directory, files: None)
        self.executor.runtime.stage = lambda request, resource: self.bundles.stage(request['spec'], request['resource_id'], resource)

    def test_staged_bundle_is_journaled_without_lifecycle_effect_or_generation_increment(self):
        self.prepare_stage()
        result = self.execute()
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(result['bundle']['state'], 'staged')
        self.assertEqual(result['generation'], 0)
        self.assertEqual(result, self.execute())
        self.assertEqual(self.effects, [])
        self.assertEqual(len(self.executor.events_after(0, self.resource)), 1)

    def test_staging_crash_after_bundle_write_reconciles_same_intent(self):
        self.prepare_stage()
        original = self.executor.runtime.stage
        def interrupted(request, resource):
            original(request, resource)
            raise KeyboardInterrupt('death after atomic bundle write, before outcome')
        self.executor.runtime.stage = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.execute()
        self.executor.runtime.stage = original
        result = self.execute()
        self.assertEqual(result['status'], 'succeeded', result)
        self.assertEqual(result['generation'], 0)
        self.assertEqual(self.effects, [])

    def test_stage_rejects_untrusted_spec_and_raw_files_before_writes(self):
        self.prepare_stage()
        for changed in [{'files': {'foreign.service': '[Service]'}}, {'spec': {**self.request['spec'], 'privileged': True}}]:
            with self.subTest(changed=changed):
                result = self.execute({**self.request, **changed})
                self.assertEqual(result['status'], 'denied', result)
        self.assertFalse((Path(self.directory.name) / 'releases').exists())
        self.assertEqual(self.effects, [])

    def test_staging_changed_spec_cannot_reuse_an_idempotency_key(self):
        self.prepare_stage()
        self.execute()
        result = self.execute({**self.request, 'spec': {**self.request['spec'], 'command': ['true']}})
        self.assertEqual(result['code'], 'idempotency_conflict')

    def test_rejected_generator_has_durable_denial_and_no_activation(self):
        self.prepare_stage()
        def reject(request, resource):
            raise self.module.BundleRejected('generator rejected candidate')
        self.executor.runtime.stage = reject
        result = self.execute()
        self.assertEqual(result['code'], 'bundle_validation_failed')
        self.assertEqual(result['status'], 'denied')
        self.assertEqual(result, self.execute())
        self.assertEqual(self.effects, [])

    def test_scope_policy_epoch_generation_and_deadline_are_enforced_before_effects(self):
        cases = [({'resource_id': str(uuid.uuid4())}, 'policy_denied'),
                 ({'action': 'shell', 'command': 'id'}, 'invalid_request'),
                 ({'policy_version': 0}, 'stale_policy'),
                 ({'controller_epoch': 6}, 'stale_controller'),
                 ({'expected_generation': 0}, 'stale_generation'),
                 ({'deadline': int(time.time()) - 1}, 'deadline_expired')]
        for changes, expected in cases:
            with self.subTest(changes=changes):
                outcome = self.execute({**self.request, **changes})
                self.assertEqual(outcome['code'], expected)
        self.assertEqual(self.effects, [])
        self.assertEqual(self.execute(principal='unknown')['code'], 'policy_denied')

    def test_idempotency_collision_cannot_change_effect(self):
        self.execute()
        result = self.execute({**self.request, 'action': 'start'})
        self.assertEqual(result['code'], 'idempotency_conflict')
        self.assertEqual(self.effects, ['stop'])

    def test_already_converged_operation_has_no_effect_or_generation_increment(self):
        self.observed['active'] = False
        result = self.execute()
        self.assertEqual(result['generation'], 1)
        self.assertEqual(self.effects, [])

    def test_crash_after_effect_is_reconciled_without_replaying_restart(self):
        self.request['action'] = 'restart'
        original = self.executor.runtime.apply

        def interrupted(action, resource):
            original(action, resource)
            raise KeyboardInterrupt('simulated node process death after effect')

        self.executor.runtime.apply = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.execute()
        result = self.execute()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(self.effects, ['restart'])
        self.assertEqual(result['generation'], 2)

    def test_ambiguous_interruption_does_not_repeat_or_allow_another_mutation(self):
        def interrupted(action, resource):
            raise KeyboardInterrupt('simulated death before an unobserved effect')

        self.executor.runtime.apply = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.execute()
        self.assertEqual(self.execute()['status'], 'needs_intervention')
        other = {**self.request, 'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4())}
        self.assertEqual(self.execute(other)['code'], 'operation_pending')
        self.assertEqual(self.effects, [])

    def test_outcome_and_event_survive_a_new_executor_process(self):
        result = self.execute()
        self.executor = self.module.Executor(Path(self.directory.name), self.executor.runtime)
        self.addCleanup(self.executor.db.close)
        self.assertEqual(self.execute(), result)
        events = self.executor.events_after(0)
        self.assertEqual(len(events), 1)
        self.assertEqual(json.loads(events[0]['payload'])['operation_id'], self.request['operation_id'])
        self.assertEqual(self.executor.events_after(events[0]['sequence']), [])

    def test_observation_outage_can_reconcile_later_without_replaying_effect(self):
        original_apply = self.executor.runtime.apply
        original_inspect = self.executor.runtime.inspect

        def outage(resource):
            raise OSError('temporary manager observation outage')

        def apply(action, resource):
            original_apply(action, resource)
            self.executor.runtime.inspect = outage

        self.executor.runtime.apply = apply
        self.assertEqual(self.execute()['status'], 'needs_intervention')
        self.assertEqual(self.execute()['status'], 'needs_intervention')
        self.assertEqual(len(self.executor.events_after(0)), 1)
        self.executor.runtime.inspect = original_inspect
        self.assertEqual(self.execute()['status'], 'succeeded')
        self.assertEqual(self.effects, ['stop'])
        self.assertEqual(len(self.executor.events_after(0)), 2)

    def test_missing_or_empty_journal_never_silently_resets_generations(self):
        missing = Path(self.directory.name) / 'missing'
        missing.mkdir()
        with self.assertRaises(ValueError):
            self.module.Executor.open_existing(missing, self.executor.runtime)
        (missing / 'journal.sqlite').write_bytes(b'')
        with self.assertRaises(ValueError):
            self.module.Executor.open_existing(missing, self.executor.runtime)
        self.execute()
        recovered = self.module.Executor.open_existing(Path(self.directory.name), self.executor.runtime)
        self.addCleanup(recovered.db.close)
        result = recovered.execute({**self.request, 'operation_id': str(uuid.uuid4()),
                                    'idempotency_key': str(uuid.uuid4())}, 'controller-a', self.policy)
        self.assertEqual(result['code'], 'stale_generation')

    def test_read_only_observation_discovers_generation_without_guessing_or_resetting_it(self):
        self.execute()
        request = {**self.request, 'operation_id': str(uuid.uuid4()),
                   'idempotency_key': str(uuid.uuid4()), 'action': 'status', 'expected_generation': 0}
        result = self.execute(request)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['generation'], 2)
        self.assertEqual(self.effects, ['stop'])
        self.assertEqual(self.execute({**request, 'action': 'start',
                                     'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4())})['code'],
                         'stale_generation')

    def test_authorized_outbox_is_scoped_replayable_and_does_not_emit_poll_events(self):
        result = self.execute()
        foreign = str(uuid.uuid4())
        with self.executor.db:
            self.executor.db.execute('INSERT INTO outbox(resource_id,payload) VALUES (?,?)',
                                     (foreign, json.dumps({'private': 'foreign event'})))
        request = {**self.request, 'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4()),
                   'action': 'events', 'expected_generation': 0, 'cursor': 0}
        self.assertEqual(self.execute(request)['code'], 'policy_denied')
        self.policy['principals']['controller-a']['actions'].append('events')
        page = self.execute(request)
        self.assertEqual(page['status'], 'succeeded', page)
        self.assertEqual(len(page['events']), 1)
        self.assertEqual(page['events'][0]['payload'], result)
        self.assertEqual(self.execute(request), page)
        following = {**request, 'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4()),
                     'cursor': page['next_cursor']}
        self.assertEqual(self.execute(following)['events'], [])
        self.assertEqual(len(self.executor.events_after(0)), 2)
        self.assertEqual(self.effects, ['stop'])

    def test_protocol_two_binds_runtime_identity_and_fences_legacy_requests(self):
        self.policy.update(minimum_protocol=2, execution_uid=1001, execution_gid=1001)
        self.assertEqual(self.execute()['code'], 'unsupported_protocol')
        request = {**self.request, 'protocol': 2, 'execution_uid': 1001, 'execution_gid': 1001}
        self.assertEqual(self.execute({**request, 'execution_uid': 1002})['code'], 'runtime_identity_mismatch')
        self.assertEqual(self.execute({**request, 'execution_gid': 1002})['code'], 'runtime_identity_mismatch')
        self.assertEqual(self.effects, [])
        self.assertEqual(self.execute(request)['status'], 'succeeded')

    def recovery_request(self):
        return {**self.request, 'operation_id': str(uuid.uuid4()), 'idempotency_key': str(uuid.uuid4()),
                'action': 'recover', 'controller_epoch': 8, 'policy_version': 2,
                'target_operation_id': self.request['operation_id']}

    def handoff(self):
        self.policy['version'] = 2
        self.policy['controller_epoch'] = 8
        self.policy['principals'] = {'controller-b': {'resources': [self.resource], 'actions': ['status', 'recover']}}

    def test_new_owner_reconciles_an_interrupted_old_intent_without_reissuing_its_effect(self):
        self.request['action'] = 'restart'
        apply = self.executor.runtime.apply

        def interrupted(action, resource):
            apply(action, resource)
            raise KeyboardInterrupt('lost controller after effect')

        self.executor.runtime.apply = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.execute()
        self.handoff()
        self.assertEqual(self.execute()['code'], 'policy_denied')
        recovery = self.recovery_request()
        outcome = self.execute(recovery, 'controller-b')
        self.assertEqual(outcome['status'], 'succeeded', outcome)
        self.assertEqual(outcome['generation'], 2)
        self.assertEqual(outcome['recovered_operation']['operation_id'], self.request['operation_id'])
        self.assertEqual(outcome['recovered_operation']['status'], 'succeeded')
        self.assertEqual(self.execute(recovery, 'controller-b'), outcome)
        self.assertEqual(self.effects, ['restart'])

    def test_recovery_cannot_cross_resource_scope_or_assume_a_changed_definition(self):
        self.request['action'] = 'restart'
        def interrupted(action, resource):
            raise KeyboardInterrupt('unknown effect')
        self.executor.runtime.apply = interrupted
        with self.assertRaises(KeyboardInterrupt):
            self.execute()
        self.handoff()
        recovery = self.recovery_request()
        unknown = {**recovery, 'target_operation_id': str(uuid.uuid4())}
        self.assertEqual(self.execute(unknown, 'controller-b')['code'], 'operation_not_found')
        self.policy['resources'][self.resource]['unit'] = 'coolify-other.service'
        self.assertEqual(self.execute(recovery, 'controller-b')['status'], 'needs_intervention')
        self.assertEqual(self.effects, [])

    def test_journal_retains_the_complete_authorized_request_for_offline_recovery(self):
        self.execute()
        row = self.executor.db.execute('SELECT principal,request FROM operations WHERE id=?',
                                       (self.request['operation_id'],)).fetchone()
        self.assertEqual(row['principal'], 'controller-a')
        self.assertEqual(json.loads(row['request']), self.request)


if __name__ == '__main__':
    unittest.main()
