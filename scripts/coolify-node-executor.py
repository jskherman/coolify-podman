#!/usr/bin/env python3
"""Restricted rootless systemd lifecycle protocol. Install through trusted bootstrap.

SSH forced command: /usr/bin/python3 -I /usr/local/libexec/coolify-node-executor.py
--principal <policy-principal>. No caller-supplied unit, path or command is accepted.
"""
import argparse
from contextlib import closing
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import uuid


FIELDS = {'protocol', 'operation_id', 'idempotency_key', 'resource_id', 'action',
          'expected_generation', 'controller_epoch', 'policy_version', 'deadline'}
ACTIONS = {'status', 'start', 'stop', 'restart', 'recover', 'events', 'stage', 'activate'}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


class BundleRejected(ValueError):
    """The pinned target generator rejected the candidate before publication."""


class QuadletBundle:
    VERSION = 'prebuilt-v1.0'

    def __init__(self, directory, validate):
        self.directory = directory
        self.validate = validate

    @staticmethod
    def encoded(value):
        return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)

    @classmethod
    def compile(cls, spec, resource_id, policy):
        def require(condition):
            if not condition:
                raise ValueError('Unsupported specification or protected profile restriction')

        def shape(value, keys):
            require(type(value) is dict and set(value) == set(keys.split()))

        def integer(value, lower, upper):
            require(type(value) is int and lower <= value <= upper)

        def text(value, maximum, pattern=None, allow_empty=False):
            require(type(value) is str and len(value) <= maximum and (allow_empty or value)
                    and not re.search(r'[\x00-\x1f\x7f$%]', value))
            if pattern:
                require(re.fullmatch(pattern, value) is not None)

        require(type(resource_id) is str and str(uuid.UUID(resource_id)) == resource_id)
        shape(spec, 'schema_version profile source image command entrypoint environment health limits network ports volumes dependencies restart_policy update_strategy migration_classification ingress labels extensions')
        integer(spec['schema_version'], 1, 1)
        require(spec['profile'] == policy.get('profile') == 'prebuilt-v1' and policy.get('kind') == 'quadlet')
        shape(spec['source'], 'type')
        require(spec['source']['type'] == 'image')
        text(spec['image'], 512, r'[a-z0-9][a-z0-9.-]*(?::[0-9]+)?/[a-z0-9][a-z0-9/._-]*@sha256:[a-f0-9]{64}')
        require(spec['image'].split('/')[0] in policy.get('image_registries', []))
        for key, maximum in [('command', 64), ('entrypoint', 16)]:
            require(type(spec[key]) is list and len(spec[key]) <= maximum)
            for argument in spec[key]:
                text(argument, 1024)
        for key in ['environment', 'dependencies', 'ingress', 'extensions']:
            require(spec[key] == [] and type(spec[key]) is list)
        shape(spec['health'], 'path status timeout_seconds')
        text(spec['health']['path'], 256, r'/[a-zA-Z0-9/._-]*')
        integer(spec['health']['status'], 200, 299)
        integer(spec['health']['timeout_seconds'], 1, 10)
        shape(spec['limits'], 'memory_mib cpu_millis')
        integer(spec['limits']['memory_mib'], 32, min(4096, policy.get('max_memory_mib', 0)))
        integer(spec['limits']['cpu_millis'], 100, min(4000, policy.get('max_cpu_millis', 0)))
        shape(spec['network'], 'internal')
        require(spec['network']['internal'] is True)
        require(type(spec['ports']) is list and len(spec['ports']) == 1)
        port = spec['ports'][0]
        shape(port, 'bind host container')
        require(port['bind'] == '127.0.0.1' and port['host'] in policy.get('host_ports', []))
        integer(port['host'], 1024, 65535)
        integer(port['container'], 1, 65535)
        require(type(spec['volumes']) is list and len(spec['volumes']) <= min(8, policy.get('max_volumes', -1)))
        names, targets = set(), set()
        for volume in spec['volumes']:
            shape(volume, 'name target read_only')
            text(volume['name'], 32, r'[a-z][a-z0-9-]*')
            text(volume['target'], 256, r'/[a-zA-Z0-9_.-]+(?:/[a-zA-Z0-9_.-]+)*')
            require(not {'.', '..'}.intersection(volume['target'].split('/')))
            require(type(volume['read_only']) is bool and volume['name'] not in names and volume['target'] not in targets)
            names.add(volume['name'])
            targets.add(volume['target'])
        require(spec['restart_policy'] == 'always' and spec['update_strategy'] == 'recreate')
        require(spec['migration_classification'] in ['none', 'backward-compatible', 'forward-only', 'unknown'])
        require(type(spec['labels']) is list and len(spec['labels']) <= 32)
        names = set()
        for label in spec['labels']:
            shape(label, 'name value')
            text(label['name'], 128, r'[a-z][a-z0-9_.-]*')
            text(label['value'], 512, allow_empty=True)
            require(not label['name'].startswith(('coolify.', 'io.containers.')) and label['name'] not in names)
            names.add(label['name'])
        for field in ['volumes', 'labels']:
            require(spec[field] == sorted(spec[field], key=lambda item: item['name']))
        name = f'coolify-{resource_id}'
        digest = hashlib.sha256(cls.encoded(spec).encode()).hexdigest()
        header = f'# Coolify managed resource {resource_id}\n# Compiler {cls.VERSION}\n# Spec {digest}\n'
        labels = f'Label=coolify.managed=true\nLabel=coolify.resource={resource_id}\n'
        files = {f'{name}.network': header + f'[Network]\nNetworkName={name}\nInternal=true\n' + labels}
        container = header + f'[Unit]\nDescription=Coolify application {resource_id}\n\n[Container]\nImage={spec["image"]}\nContainerName={name}\nNetwork={name}.network\n'
        container += f'PublishPort=127.0.0.1:{port["host"]}:{port["container"]}\n' + labels
        for label in spec['labels']:
            container += 'Label=' + cls.encoded(label['name'] + '=' + label['value']) + '\n'
        if spec['entrypoint']:
            container += 'Entrypoint=' + cls.encoded(spec['entrypoint']) + '\n'
        if spec['command']:
            container += 'Exec=' + ' '.join(cls.encoded(arg) for arg in spec['command']) + '\n'
        for volume in spec['volumes']:
            volume_name = name + '-' + volume['name']
            files[f'{volume_name}.volume'] = header + f'[Volume]\nVolumeName={volume_name}\n' + labels
            container += f'Volume={volume_name}.volume:{volume["target"]}:' + ('ro' if volume['read_only'] else 'rw') + '\n'
        quota, fraction = divmod(spec['limits']['cpu_millis'], 10)
        cpu = str(quota) + (f'.{fraction}' if fraction else '') + '%'
        container += f'\n[Service]\nRestart=always\nTimeoutStartSec=120\nMemoryMax={spec["limits"]["memory_mib"]}M\nCPUQuota={cpu}\n\n[Install]\nWantedBy=default.target\n'
        files[f'{name}.container'] = container
        return dict(sorted(files.items()))

    @staticmethod
    def sync_directory(path):
        descriptor = os.open(path, os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def stage(self, spec, resource_id, policy):
        files = self.compile(spec, resource_id, policy)
        bundle_hash = hashlib.sha256(self.encoded(files).encode()).hexdigest()
        metadata = {'state': 'staged', 'resource_id': resource_id, 'compiler_version': self.VERSION,
                    'spec_hash': hashlib.sha256(self.encoded(spec).encode()).hexdigest(), 'bundle_hash': bundle_hash}
        if 'generator_sha256' in policy and 'podman_version' in policy:
            metadata['validation'] = {key: policy[key] for key in ['generator_sha256', 'podman_version']}
        record = self.encoded({'metadata': metadata, 'spec': spec}) + '\n'
        parent = self.directory / resource_id
        for path in [self.directory, parent]:
            path.mkdir(mode=0o700, exist_ok=True)
            info = path.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise ValueError('Release directory is not protected')
            self.sync_directory(path.parent)
        release = parent / bundle_hash
        expected = {**files, 'manifest.json': record}
        if release.exists() or release.is_symlink():
            metadata_on_disk = release.lstat()
            if (not stat.S_ISDIR(metadata_on_disk.st_mode) or metadata_on_disk.st_uid != os.getuid()
                    or metadata_on_disk.st_mode & 0o077 or set(item.name for item in release.iterdir()) != set(expected)):
                raise ValueError('Existing release is not the expected complete bundle')
            for name, contents in expected.items():
                path = release / name
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(descriptor, 'r', encoding='utf-8') as stream:
                    info = os.fstat(stream.fileno())
                    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
                            or info.st_nlink != 1 or info.st_size != len(contents.encode()) or stream.read() != contents):
                        raise ValueError('Existing immutable release changed or is not protected')
            self.sync_directory(release)
            self.sync_directory(parent)
            return metadata
        staging = Path(tempfile.mkdtemp(prefix='.staging-', dir=parent))
        try:
            for name, contents in files.items():
                with (staging / name).open('x', encoding='utf-8') as stream:
                    os.chmod(stream.fileno(), 0o600)
                    stream.write(contents)
                    stream.flush()
                    os.fsync(stream.fileno())
            self.validate(staging, files)
            with (staging / 'manifest.json').open('x', encoding='utf-8') as stream:
                os.chmod(stream.fileno(), 0o600)
                stream.write(record)
                stream.flush()
                os.fsync(stream.fileno())
            self.sync_directory(staging)
            staging.rename(release)
            self.sync_directory(parent)
            self.sync_directory(self.directory)
            return metadata
        finally:
            if staging.exists():
                shutil.rmtree(staging)


def denied(code):
    remediation = {
        'unsupported_protocol': 'Upgrade the controller to the minimum protocol required by node policy.',
        'runtime_identity_mismatch': 'Re-probe and reconcile the runtime identity before submitting another intent.',
        'invalid_request': 'Correct the protocol fields; no effect was started.',
        'policy_denied': 'A trusted operator must grant this principal the required resource and action scope.',
        'stale_policy': 'Read the current authorized policy before submitting a new intent.',
        'stale_controller': 'Reconcile controller ownership; do not retry with another transport.',
        'stale_generation': 'Observe the managed resource generation before submitting a new intent.',
        'deadline_expired': 'Reconcile recorded operations before creating a new intent with a current deadline.',
        'executor_busy': 'Retry the same operation after the current executor lock is released.',
        'idempotency_conflict': 'Use the original immutable request for this operation and idempotency key.',
        'recovery_chain_too_deep': 'Recover the original scoped lifecycle operation instead of nesting more recovery handles.',
        'operation_not_found': 'No matching operation exists in this authorized resource scope.',
        'operation_pending': 'Reconcile the unresolved operation before another mutation.',
        'observation_unavailable': 'Restore access to the unchanged managed unit and its protected definition.',
        'restricted_transport_required': 'Use the dedicated restricted SSH credential and protocol command.',
        'executor_unavailable': 'Inspect node installation and reconcile the operation journal before retrying.',
        'unsupported_spec': 'Use the declared native profile within the resource limits granted by node policy.',
        'bundle_validation_failed': 'Correct the candidate for the pinned node generator; no active configuration was changed.',
        'activation_rejected': 'Reconcile staged artifacts, effective units, owned objects and runtime capabilities before activation.',
        'image_acquisition_failed': 'Correct registry availability or image authorization before submitting a new activation intent.',
    }
    return {'status': 'denied', 'code': code, 'retryable': code == 'executor_busy',
            'retry_when': remediation[code]}


class Executor:
    @classmethod
    def open_existing(cls, directory, runtime):
        database = directory / 'journal.sqlite'
        if not database.is_file() or database.is_symlink():
            raise ValueError('Initialized journal is missing; trusted recovery is required')
        try:
            with closing(sqlite3.connect(f'file:{database}?mode=ro', uri=True)) as connection:
                version = connection.execute("SELECT value FROM metadata WHERE name='schema_version'").fetchone()
                if version != ('2',):
                    raise ValueError('Journal schema is unsupported')
        except sqlite3.Error as exception:
            raise ValueError('Journal metadata is missing or corrupt') from exception
        return cls(directory, runtime, create=False)

    def __init__(self, directory, runtime, create=True):
        self.directory = directory
        self.runtime = runtime
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        database = directory / 'journal.sqlite'
        self.db = (sqlite3.connect(database, timeout=5) if create else
                   sqlite3.connect(f'file:{database}?mode=rw', uri=True, timeout=5))
        self.db.row_factory = sqlite3.Row
        if not create:
            version = self.db.execute("SELECT value FROM metadata WHERE name='schema_version'").fetchone()
            if version is None or version[0] != '2':
                raise ValueError('Journal schema changed during opening')
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        if create:
            self.db.executescript('''
            CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT OR IGNORE INTO metadata VALUES ('schema_version', '2');
            CREATE TABLE IF NOT EXISTS resources (id TEXT PRIMARY KEY, generation INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL,
                resource_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
                state TEXT NOT NULL, before_state TEXT, outcome TEXT,
                principal TEXT NOT NULL, request TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT, resource_id TEXT NOT NULL,
                payload TEXT NOT NULL);
            ''')
        self.db.commit()
        descriptor = os.open(directory, os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def execute(self, request, principal, policy):
        if not isinstance(request, dict):
            return denied('invalid_request')
        fields = FIELDS | ({'target_operation_id'} if request.get('action') == 'recover' else set())
        fields |= {'cursor'} if request.get('action') == 'events' else set()
        fields |= {'spec'} if request.get('action') == 'stage' else set()
        fields |= {'bundle_hash'} if request.get('action') == 'activate' else set()
        fields |= {'execution_uid', 'execution_gid'} if request.get('protocol') == 2 else set()
        if set(request) != fields:
            return denied('invalid_request')
        try:
            for key in ['operation_id', 'idempotency_key', 'resource_id'] + (['target_operation_id'] if request.get('action') == 'recover' else []):
                if str(uuid.UUID(request[key])) != request[key]:
                    return denied('invalid_request')
            for key in ['protocol', 'expected_generation', 'controller_epoch', 'policy_version', 'deadline'] + (['cursor'] if request.get('action') == 'events' else []) + (['execution_uid', 'execution_gid'] if request.get('protocol') == 2 else []):
                if type(request[key]) is not int or request[key] < 0:
                    return denied('invalid_request')
        except (ValueError, TypeError, AttributeError):
            return denied('invalid_request')
        if request['protocol'] not in {1, 2} or not isinstance(request['action'], str) or request['action'] not in ACTIONS:
            return denied('invalid_request')
        if request['action'] == 'activate' and (not isinstance(request['bundle_hash'], str)
                                                or not re.fullmatch(r'[a-f0-9]{64}', request['bundle_hash'])):
            return denied('invalid_request')
        with (self.directory / 'executor.lock').open('a') as lock:
            end = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= end:
                        return denied('executor_busy')
                    time.sleep(0.05)
            policy = policy() if callable(policy) else policy
            if request['protocol'] < policy.get('minimum_protocol', 1):
                return denied('unsupported_protocol')
            if request['action'] in {'stage', 'activate'} and request['protocol'] != 2:
                return denied('unsupported_protocol')
            if request['protocol'] == 2 and (request['execution_uid'] != policy.get('execution_uid')
                                              or request['execution_gid'] != policy.get('execution_gid')):
                return denied('runtime_identity_mismatch')
            grant = policy.get('principals', {}).get(principal, {})
            resource_id = request['resource_id']
            if (resource_id not in grant.get('resources', []) or request['action'] not in grant.get('actions', [])
                    or resource_id not in policy.get('resources', {})):
                return denied('policy_denied')
            if request['policy_version'] != policy['version']:
                return denied('stale_policy')
            if request['controller_epoch'] != policy['controller_epoch']:
                return denied('stale_controller')
            resource = policy['resources'][resource_id]
            if resource.get('kind') == 'quadlet':
                resource = {**resource, '_resource_id': resource_id}
            return self._locked(request, resource, principal)

    def _locked(self, request, resource, principal):
        resource_id = request['resource_id']
        payload_hash = hashlib.sha256(canonical([principal, request]).encode()).hexdigest()
        previous = self.db.execute('SELECT * FROM operations WHERE id=? OR idempotency_key=?',
                                   (request['operation_id'], request['idempotency_key'])).fetchone()
        if previous:
            if previous['payload_hash'] != payload_hash:
                return denied('idempotency_conflict')
            if previous['outcome'] and previous['state'] in {'succeeded', 'failed', 'denied'}:
                return json.loads(previous['outcome'])
            return self._reconcile(request, resource, json.loads(previous['before_state']))
        if request['deadline'] <= int(time.time()):
            return denied('deadline_expired')
        pending = self.db.execute("SELECT id FROM operations WHERE resource_id=? AND state IN ('executing','needs_intervention')", (resource_id,)).fetchone()
        if pending and request['action'] not in {'status', 'recover', 'events'}:
            return {**denied('operation_pending'), 'blocking_operation_id': pending['id']}
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO resources VALUES (?,?)', (resource_id, resource['generation']))
        generation = self.db.execute('SELECT generation FROM resources WHERE id=?', (resource_id,)).fetchone()[0]
        if request['action'] not in {'status', 'events'} and generation != request['expected_generation']:
            return denied('stale_generation')
        if request['action'] == 'activate':
            try:
                if resource.get('kind') != 'quadlet':
                    raise ValueError('Native resource grant required')
                native = self.runtime.native
                candidate = native.load(resource_id, request['bundle_hash'], resource)
                observed = native.preflight(resource_id, request['bundle_hash'], resource)
                previous_hash = native.current(resource_id, resource)
                if previous_hash:
                    prior = native.load(resource_id, previous_hash, resource)
                    if any(candidate['spec'][field] != prior['spec'][field] for field in ['volumes', 'network', 'ports']):
                        raise ValueError('Storage or exposure changes require an explicit migration')
                before = {'phase': 'planned', 'previous_bundle_hash': previous_hash,
                          'before_active': observed['active'], 'before_health': observed['health'],
                          'definition_hash': self.definition_hash(resource),
                          'no_op': previous_hash == request['bundle_hash'] and observed['active'] and observed['health']}
            except Exception:
                return denied('activation_rejected')
            with self.db:
                self.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,NULL,?,?)',
                                (request['operation_id'], request['idempotency_key'], resource_id,
                                 payload_hash, 'executing', canonical(before), principal, canonical(request)))
            return self._reconcile(request, resource, before)
        if request['action'] == 'stage':
            try:
                QuadletBundle.compile(request['spec'], resource_id, resource)
            except (ValueError, TypeError, KeyError):
                return denied('unsupported_spec')
            before = {'definition_hash': self.definition_hash(resource)}
            with self.db:
                self.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,NULL,?,?)',
                                (request['operation_id'], request['idempotency_key'], resource_id,
                                 payload_hash, 'executing', canonical(before), principal, canonical(request)))
            return self._reconcile(request, resource, before)
        if request['action'] == 'events':
            with self.db:
                self.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,NULL,?,?)',
                                (request['operation_id'], request['idempotency_key'], resource_id,
                                 payload_hash, 'executing', '{}', principal, canonical(request)))
            return self._reconcile(request, resource, {})
        if request['action'] == 'recover':
            target = self.db.execute('SELECT * FROM operations WHERE id=? AND resource_id=?',
                                     (request['target_operation_id'], resource_id)).fetchone()
            if target is None or json.loads(target['request'])['action'] not in {'start', 'stop', 'restart', 'activate', 'recover'}:
                return denied('operation_not_found')
            cursor = target
            for _ in range(8):
                intent = json.loads(cursor['request'])
                if intent['action'] != 'recover':
                    break
                cursor = self.db.execute('SELECT * FROM operations WHERE id=? AND resource_id=?',
                                         (intent['target_operation_id'], resource_id)).fetchone()
                if cursor is None:
                    return denied('operation_not_found')
            else:
                return denied('recovery_chain_too_deep')
        try:
            before = {**self.runtime.inspect(resource), 'definition_hash': self.definition_hash(resource)}
        except Exception:
            return denied('observation_unavailable')
        if request['deadline'] <= int(time.time()):
            return denied('deadline_expired')
        with self.db:
            self.db.execute('INSERT INTO operations VALUES (?,?,?,?,?,?,NULL,?,?)',
                            (request['operation_id'], request['idempotency_key'], resource_id,
                             payload_hash, 'executing', canonical(before), principal, canonical(request)))
        action = request['action']
        if action == 'recover':
            return self._reconcile(request, resource, before)
        if action == 'status' or (action == 'start' and before['active']) or (action == 'stop' and not before['active']):
            return self._finish(request, before, changed=False)
        try:
            self.runtime.apply(action, resource)
        except Exception:
            # A timeout or lost command reply does not prove the effect failed.
            pass
        return self._reconcile(request, resource, before)

    @staticmethod
    def definition_hash(resource):
        return hashlib.sha256(canonical({key: value for key, value in resource.items() if key not in {'generation', '_resource_id', 'native_adapter_sha256'}}).encode()).hexdigest()

    def _reconcile(self, request, resource, before):
        if request['action'] == 'activate':
            return self._activate(request, resource, before)
        if request['action'] == 'stage':
            if before.get('definition_hash') != self.definition_hash(resource):
                return self._unresolved(request, 'recovery_definition_mismatch')
            try:
                bundle = self.runtime.stage(request, resource)
            except BundleRejected:
                outcome = {**denied('bundle_validation_failed'), 'operation_id': request['operation_id'],
                           'resource_id': request['resource_id']}
                with self.db:
                    self.db.execute('UPDATE operations SET state=?,outcome=? WHERE id=?',
                                    ('denied', canonical(outcome), request['operation_id']))
                    self.db.execute('INSERT INTO outbox(resource_id,payload) VALUES (?,?)',
                                    (request['resource_id'], canonical(outcome)))
                return outcome
            except Exception:
                return self._unresolved(request, 'bundle_staging_unresolved')
            return self._finish(request, {}, changed=False, extra={'bundle': bundle})
        if request['action'] == 'events':
            rows = self.events_after(request['cursor'], request['resource_id'])
            events = [{**row, 'payload': json.loads(row['payload'])} for row in rows]
            return self._finish(request, {}, changed=False, extra={'events': events,
                                'next_cursor': rows[-1]['sequence'] if rows else request['cursor']})
        if request['action'] == 'recover':
            target = self.db.execute('SELECT * FROM operations WHERE id=? AND resource_id=?',
                                     (request['target_operation_id'], request['resource_id'])).fetchone()
            if target['state'] in {'succeeded', 'failed'}:
                recovered = json.loads(target['outcome'])
            else:
                original_before = json.loads(target['before_state'])
                if original_before.get('definition_hash') != self.definition_hash(resource):
                    return self._unresolved(request, 'recovery_definition_mismatch')
                recovered = self._reconcile(json.loads(target['request']), resource, original_before)
            if recovered['status'] not in {'succeeded', 'failed'}:
                return self._unresolved(request, 'recovery_target_unresolved')
            try:
                observed = self.runtime.inspect(resource)
            except Exception:
                return self._unresolved(request, 'observation_unavailable')
            return self._finish(request, observed, changed=False, extra={'recovered_operation': recovered})
        if before.get('definition_hash') is not None and before['definition_hash'] != self.definition_hash(resource):
            return self._unresolved(request, 'recovery_definition_mismatch')
        try:
            observed = self.runtime.inspect(resource)
            action = request['action']
            converged = ((action == 'stop' and not observed['active']) or
                         (action == 'start' and observed['active']) or
                         (action == 'restart' and observed['active'] and observed['invocation']
                          and observed['invocation'] != before['invocation']))
            if action == 'status':
                return self._finish(request, observed, changed=False)
            if converged:
                return self._finish(request, observed, changed=True)
        except Exception:
            pass
        return self._unresolved(request, 'effect_unresolved')

    def _checkpoint(self, request, before, phase, **updates):
        before.update(phase=phase, **updates)
        with self.db:
            self.db.execute('UPDATE operations SET before_state=?,state=?,outcome=NULL WHERE id=?',
                            (canonical(before), 'executing', request['operation_id']))

    def _activation_result(self, request, before, state, **extra):
        return {'state': state, 'bundle_hash': request['bundle_hash'],
                'previous_bundle_hash': before['previous_bundle_hash'], **extra}

    def _running(self, request, before):
        generation = self.db.execute('SELECT generation FROM resources WHERE id=?', (request['resource_id'],)).fetchone()[0]
        state = 'compensating' if before['phase'].startswith('compensating') else 'activating'
        outcome = {'status': 'executing', 'operation_id': request['operation_id'],
                   'resource_id': request['resource_id'], 'generation': generation,
                   'release': self._activation_result(request, before, state)}
        with self.db:
            row = self.db.execute('SELECT outcome FROM operations WHERE id=?', (request['operation_id'],)).fetchone()
            if row['outcome'] != canonical(outcome):
                self.db.execute('UPDATE operations SET state=?,outcome=? WHERE id=?',
                                ('executing', canonical(outcome), request['operation_id']))
                self.db.execute('INSERT INTO outbox(resource_id,payload) VALUES (?,?)',
                                (request['resource_id'], canonical(outcome)))
        return outcome

    def _activate(self, request, resource, before):
        if before.get('definition_hash') != self.definition_hash(resource):
            return self._unresolved(request, 'recovery_definition_mismatch')
        try:
            native = self.runtime.native
            resource_id = request['resource_id']
            candidate = native.load(resource_id, request['bundle_hash'], resource)
            timeout = resource.get('activation_timeout_seconds', 60)
            if type(timeout) is not int or not 10 <= timeout <= 120:
                raise ValueError('Invalid protected activation timeout')
            if before['no_op']:
                observed = native.inspect(resource_id, resource, request['bundle_hash'])
                if observed['active'] and observed['health']:
                    return self._finish(request, observed, False, {'release': self._activation_result(request, before, 'committed', no_op=True)})
                return self._unresolved(request, 'no_op_observation_changed')
            for _ in range(8):
                phase = before['phase']
                if phase == 'planned':
                    if native.current(resource_id, resource) != before['previous_bundle_hash']:
                        raise ValueError('Selection changed before activation')
                    image = native.image(resource_id, request['operation_id'], candidate['spec'], resource)
                    if image['failed']:
                        outcome = {**denied('image_acquisition_failed'), 'operation_id': request['operation_id'], 'resource_id': resource_id}
                        with self.db:
                            self.db.execute('UPDATE operations SET state=?,outcome=? WHERE id=?', ('denied', canonical(outcome), request['operation_id']))
                            self.db.execute('INSERT INTO outbox(resource_id,payload) VALUES (?,?)', (resource_id, canonical(outcome)))
                        return outcome
                    if not image['ready']:
                        return self._running(request, before)
                    self._checkpoint(request, before, 'staged')
                elif phase == 'staged':
                    native.preflight(resource_id, request['bundle_hash'], resource)
                    self._checkpoint(request, before, 'stopping')
                elif phase == 'stopping':
                    observed = native.inspect(resource_id, resource)
                    if observed['active'] or observed['transitioning']:
                        native.stop(resource_id, resource)
                        return self._running(request, before)
                    self._checkpoint(request, before, 'publishing')
                elif phase == 'publishing':
                    native.begin_update(resource_id, request['operation_id'], request['bundle_hash'], before['previous_bundle_hash'], resource)
                    if native.current(resource_id, resource) not in {None, before['previous_bundle_hash'], request['bundle_hash']}:
                        raise ValueError('Foreign selection during publication')
                    native.select(resource_id, request['bundle_hash'], resource)
                    native.reload_verify(resource_id, request['bundle_hash'], resource)
                    self._checkpoint(request, before, 'activating',
                                     activation_initial_invocation=native.inspect(resource_id, resource, request['bundle_hash'])['invocation'])
                elif phase == 'activating':
                    if native.interrupted_update(resource_id, request['operation_id'], resource):
                        safe = candidate['spec']['migration_classification'] in {'none', 'backward-compatible'}
                        if before['previous_bundle_hash'] and (not safe or (before['before_active'] and not before['before_health'])):
                            self._checkpoint(request, before, 'isolating')
                        else:
                            self._checkpoint(request, before, 'compensating_publish')
                        continue
                    observed = native.inspect(resource_id, resource, request['bundle_hash'])
                    if 'activation_initial_invocation' not in before:
                        self._checkpoint(request, before, 'activating', activation_initial_invocation=observed['invocation'])
                    if observed['active'] and observed['health']:
                        self._checkpoint(request, before, 'committed')
                        continue
                    if 'activation_started_at' in before and int(time.time()) - before['activation_started_at'] >= timeout:
                        safe = candidate['spec']['migration_classification'] in {'none', 'backward-compatible'}
                        if before['previous_bundle_hash'] and (not safe or (before['before_active'] and not before['before_health'])):
                            self._checkpoint(request, before, 'isolating')
                        else:
                            self._checkpoint(request, before, 'compensating_stop', compensation_started_at=int(time.time()))
                        continue
                    if not observed['active'] and not observed['transitioning'] and observed['invocation'] == before['activation_initial_invocation']:
                        native.start(resource_id, resource, before_request=(
                            lambda: self._checkpoint(request, before, 'activating', activation_started_at=int(time.time()))
                        ) if 'activation_started_at' not in before else None)
                    return self._running(request, before)
                elif phase in {'compensating_stop', 'isolating'}:
                    observed = native.inspect(resource_id, resource)
                    if observed['active'] or observed['transitioning']:
                        native.stop(resource_id, resource)
                        return self._running(request, before)
                    if phase == 'isolating':
                        native.select(resource_id, None, resource)
                        native.reload_verify(resource_id, None, resource)
                        return self._unresolved(request, 'unsafe_data_rollback')
                    self._checkpoint(request, before, 'compensating_publish')
                elif phase == 'compensating_publish':
                    native.select(resource_id, before['previous_bundle_hash'], resource)
                    native.reload_verify(resource_id, before['previous_bundle_hash'], resource)
                    self._checkpoint(request, before, 'compensating_start',
                                     compensation_initial_invocation=native.inspect(resource_id, resource)['invocation'])
                elif phase == 'compensating_start':
                    if native.interrupted_update(resource_id, request['operation_id'], resource):
                        before.pop('compensation_activation_started_at', None)
                        self._checkpoint(request, before, 'compensating_publish')
                        continue
                    if 'compensation_initial_invocation' not in before:
                        self._checkpoint(request, before, 'compensating_start',
                                     compensation_initial_invocation=native.inspect(resource_id, resource)['invocation'])
                    observed = native.inspect(resource_id, resource)
                    if not before['previous_bundle_hash'] or not before['before_active'] or (observed['active'] and observed['health']):
                        self._checkpoint(request, before, 'rolled_back')
                        continue
                    if 'compensation_activation_started_at' in before and int(time.time()) - before['compensation_activation_started_at'] >= timeout:
                        return self._unresolved(request, 'compensation_failed')
                    if not observed['active'] and not observed['transitioning'] and observed['invocation'] == before['compensation_initial_invocation']:
                        native.start(resource_id, resource, before_request=(
                            lambda: self._checkpoint(request, before, 'compensating_start', compensation_activation_started_at=int(time.time()))
                        ) if 'compensation_activation_started_at' not in before else None)
                    return self._running(request, before)
                elif phase == 'committed':
                    if native.current(resource_id, resource) is None and native.interrupted_update(resource_id, request['operation_id'], resource):
                        native.select(resource_id, request['bundle_hash'], resource)
                        native.reload_verify(resource_id, request['bundle_hash'], resource)
                        self._checkpoint(request, before, 'accepted_restart', accepted_initial_invocation=native.inspect(resource_id, resource)['invocation'])
                        continue
                    observed = native.inspect(resource_id, resource, request['bundle_hash'])
                    if not observed['active'] or not observed['health']:
                        return self._unresolved(request, 'commit_observation_changed')
                    observed = native.commit(resource_id, request['bundle_hash'], request['operation_id'], resource)
                    return self._finish(request, observed, True, {'release': self._activation_result(request, before, 'committed', no_op=False)})
                elif phase == 'accepted_restart':
                    if native.interrupted_update(resource_id, request['operation_id'], resource):
                        before.pop('accepted_restart_started_at', None)
                        self._checkpoint(request, before, 'committed')
                        continue
                    observed = native.inspect(resource_id, resource, request['bundle_hash'])
                    if observed['active'] and observed['health']:
                        self._checkpoint(request, before, 'committed')
                        continue
                    if 'accepted_restart_started_at' in before and int(time.time()) - before['accepted_restart_started_at'] >= timeout:
                        return self._unresolved(request, 'accepted_release_restart_failed')
                    if not observed['active'] and not observed['transitioning'] and observed['invocation'] == before['accepted_initial_invocation']:
                        native.start(resource_id, resource, before_request=(
                            lambda: self._checkpoint(request, before, 'accepted_restart', accepted_restart_started_at=int(time.time()))
                        ) if 'accepted_restart_started_at' not in before else None)
                    return self._running(request, before)
                elif phase == 'rolled_back':
                    if before['previous_bundle_hash'] and native.current(resource_id, resource) is None and native.interrupted_update(resource_id, request['operation_id'], resource):
                        native.select(resource_id, before['previous_bundle_hash'], resource)
                        native.reload_verify(resource_id, before['previous_bundle_hash'], resource)
                        before.pop('compensation_activation_started_at', None)
                        self._checkpoint(request, before, 'compensating_start', compensation_initial_invocation=native.inspect(resource_id, resource)['invocation'])
                        continue
                    observed = native.commit(resource_id, before['previous_bundle_hash'], request['operation_id'], resource)
                    return self._finish(request, observed, True,
                                        {'release': self._activation_result(request, before, 'rolled_back', data_recovery=False)}, status='failed')
                else:
                    raise ValueError('Unknown journal phase')
            return self._running(request, before)
        except Exception as exception:
            frames = [frame for frame in traceback.extract_tb(exception.__traceback__)
                      if Path(frame.filename).name in {'coolify-node-executor.py', 'coolify-node-native.py'}]
            location = frames[-1] if frames else None
            diagnostic = {'operation_id': request['operation_id'], 'phase': before.get('phase'),
                          'exception': type(exception).__name__,
                          'location': location.name if location else None,
                          'line': location.lineno if location else None,
                          'errno': exception.errno if isinstance(exception, OSError) else None}
            print(canonical(diagnostic), file=sys.stderr, flush=True)
            return self._unresolved(request, 'activation_observation_unavailable')

    def _unresolved(self, request, code):
        outcome = {'status': 'needs_intervention', 'code': code,
                   'operation_id': request['operation_id'], 'resource_id': request['resource_id']}
        with self.db:
            previous = self.db.execute('SELECT outcome FROM operations WHERE id=?', (request['operation_id'],)).fetchone()
            if previous['outcome'] == canonical(outcome):
                return outcome
            self.db.execute('UPDATE operations SET state=?,outcome=? WHERE id=?',
                            ('needs_intervention', canonical(outcome), request['operation_id']))
            self.db.execute('INSERT INTO outbox(resource_id,payload) VALUES (?,?)',
                            (request['resource_id'], canonical(outcome)))
        return outcome

    def _finish(self, request, observed, changed, extra=None, status='succeeded'):
        with self.db:
            if changed:
                self.db.execute('UPDATE resources SET generation=generation+1 WHERE id=?', (request['resource_id'],))
            generation = self.db.execute('SELECT generation FROM resources WHERE id=?', (request['resource_id'],)).fetchone()[0]
            outcome = {'status': status, 'operation_id': request['operation_id'],
                       'resource_id': request['resource_id'], 'generation': generation, 'observed': observed, **(extra or {})}
            self.db.execute('UPDATE operations SET state=?,outcome=? WHERE id=?',
                            (status, canonical(outcome), request['operation_id']))
            if request['action'] != 'events':
                self.db.execute('INSERT INTO outbox(resource_id,payload) VALUES (?,?)',
                                (request['resource_id'], canonical(outcome)))
        return outcome

    def events_after(self, sequence, resource_id=None):
        if resource_id is not None:
            return [dict(row) for row in self.db.execute(
                'SELECT * FROM outbox WHERE resource_id=? AND sequence>? ORDER BY sequence LIMIT 25', (resource_id, sequence))]
        return [dict(row) for row in self.db.execute('SELECT * FROM outbox WHERE sequence>? ORDER BY sequence LIMIT 25', (sequence,))]


class SystemdRuntime:
    def __init__(self, uid):
        self.uid = uid
        self.environment = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C',
                            'HOME': pwd.getpwuid(uid).pw_dir,
                            'XDG_RUNTIME_DIR': f'/run/user/{uid}',
                            'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/run/user/{uid}/bus'}

    @property
    def native(self):
        if not hasattr(self, '_native'):
            source = Path('/usr/local/libexec/coolify-node-native.py')
            for path in [source.parent.parent, source.parent, source]:
                metadata = path.lstat()
                if metadata.st_uid != 0 or metadata.st_mode & 0o022 or stat.S_ISLNK(metadata.st_mode):
                    raise ValueError('Native adapter is not protected')
            loader = importlib.util.spec_from_file_location('coolify_node_native', source)
            module = importlib.util.module_from_spec(loader)
            loader.loader.exec_module(module)
            self._native = module.NativeRuntime(self.uid, Path(f'/var/lib/coolify-node/{self.uid}'), QuadletBundle)
        return self._native

    def stage(self, request, resource):
        generator = Path('/usr/lib/systemd/system-generators/podman-system-generator')
        if hashlib.sha256(generator.read_bytes()).hexdigest() != resource.get('generator_sha256'):
            raise BundleRejected('Pinned generator changed; re-probe before compiling')
        version = subprocess.run(['/usr/bin/podman', '--remote=false', '--version'], env=self.environment,
                                 text=True, capture_output=True, check=True, timeout=10).stdout.strip()
        if version != 'podman version ' + resource.get('podman_version', ''):
            raise BundleRejected('Pinned Podman version changed; re-probe before compiling')
        def validate(directory, files):
            result = subprocess.run([str(generator), '--user', '--dryrun'],
                                    env={**self.environment, 'QUADLET_UNIT_DIRS': str(directory)},
                                    text=True, capture_output=True, check=False, timeout=30)
            generated = result.stdout + result.stderr
            expected = []
            for name in files:
                path = Path(name)
                suffix = {'container': '', 'network': '-network', 'volume': '-volume'}[path.suffix[1:]]
                expected.append(path.stem + suffix + '.service')
            if result.returncode != 0 or any(f'---{name}---' not in generated for name in expected):
                raise BundleRejected('Target generator did not produce the complete bundle')
        return QuadletBundle(Path(f'/var/lib/coolify-node/{self.uid}/releases'), validate).stage(
            request['spec'], request['resource_id'], resource)

    def inspect(self, resource):
        if resource.get('kind') == 'quadlet':
            return self.native.inspect(resource['_resource_id'], resource)
        unit = resource['unit']
        if not re.fullmatch(r'coolify-[a-z0-9-]+\.service', unit):
            raise ValueError('Invalid managed unit')
        source = Path(resource['source'])
        if source.is_symlink() or hashlib.sha256(source.read_bytes()).hexdigest() != resource['source_sha256']:
            raise ValueError('Managed definition changed')
        result = subprocess.run(['/usr/bin/systemctl', '--user', 'show', unit,
                                 '--property=ActiveState,SubState,InvocationID,LoadState,NeedDaemonReload'],
                                env=self.environment, text=True, capture_output=True, check=True, timeout=10)
        observed = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)
        if (observed.get('LoadState') != 'loaded' or observed.get('NeedDaemonReload') != 'no'
                or observed.get('ActiveState') not in ['active', 'inactive', 'failed']):
            raise ValueError('Managed unit is absent or still transitioning')
        effective = subprocess.run(['/usr/bin/systemctl', '--user', 'cat', unit], env=self.environment,
                                   capture_output=True, check=True, timeout=10).stdout
        if hashlib.sha256(effective).hexdigest() != resource['effective_sha256']:
            raise ValueError('Effective managed unit changed')
        return {'active': observed['ActiveState'] == 'active', 'invocation': observed.get('InvocationID', '')}

    def apply(self, action, resource):
        if action not in {'start', 'stop', 'restart'}:
            raise ValueError('Unsupported action')
        if resource.get('kind') == 'quadlet':
            observed = self.native.inspect(resource['_resource_id'], resource)
            if observed['bundle_hash'] is None:
                raise ValueError('Native lifecycle requires a selected release')
            self.native.preflight(resource['_resource_id'], observed['bundle_hash'], resource)
            unit = self.native.resource_name(resource['_resource_id']) + '.service'
        else:
            unit = resource['unit']
        subprocess.run(['/usr/bin/systemctl', '--user', action, unit],
                       env=self.environment, capture_output=True, check=True, timeout=30)


def trusted_policy(path):
    for item in [path.parent.parent, path.parent, path]:
        metadata = item.lstat()
        if metadata.st_uid != 0 or metadata.st_mode & 0o022 or stat.S_ISLNK(metadata.st_mode):
            raise ValueError('Policy is not protected')
    if path.stat().st_size > 65536:
        raise ValueError('Policy too large')
    return json.loads(path.read_text())


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('--principal', required=True)
    args = parser.parse_args()
    if (os.getuid() == 0 or not sys.flags.isolated or not os.environ.get('SSH_CONNECTION')
            or os.environ.get('SSH_ORIGINAL_COMMAND') != 'coolify-node-v1'):
        print(canonical(denied('restricted_transport_required')))
        return
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', args.principal):
        raise ValueError('Invalid principal')
    uid = os.getuid()
    def load_policy():
        policy = trusted_policy(Path(f'/etc/coolify-node/policies/{uid}.json'))
        if policy['execution_uid'] != uid or ('execution_gid' in policy and policy['execution_gid'] != os.getgid()):
            raise ValueError('Runtime identity mismatch')
        return policy
    directory = Path(f'/var/lib/coolify-node/{uid}')
    metadata = directory.lstat()
    if metadata.st_uid != uid or metadata.st_mode & 0o077 or not stat.S_ISDIR(metadata.st_mode):
        raise ValueError('Journal directory is not protected')
    raw = sys.stdin.buffer.read(16385)
    if len(raw) > 16384:
        raise ValueError('Request too large')
    executor = Executor.open_existing(directory, SystemdRuntime(uid))
    try:
        print(canonical(executor.execute(json.loads(raw), args.principal, load_policy)))
    finally:
        executor.db.close()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print(canonical(denied('executor_unavailable')))
        sys.exit(1)
