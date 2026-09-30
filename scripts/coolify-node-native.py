#!/usr/bin/env python3
"""Fixed rootless Quadlet adapter, called only under the node journal lock.

Caller inputs are resource IDs and immutable hashes. Paths, unit names, listeners
and process arguments are derived from protected policy and compiled artifacts.
"""
import hashlib
import http.client
import json
import os
from pathlib import Path
import pwd
import re
import socket
import stat
import subprocess
import tempfile
import uuid


class NativeRuntime:
    GENERATOR = Path('/usr/lib/systemd/system-generators/podman-system-generator')
    USER_GENERATOR = Path('/usr/lib/systemd/user-generators/podman-user-generator')

    def __init__(self, uid, directory, bundle_type):
        self.uid = uid
        self.directory = directory
        self.bundle_type = bundle_type
        self.home = Path(pwd.getpwuid(uid).pw_dir)
        self.runtime_directory = Path(f'/run/user/{uid}')
        self.environment = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'HOME': str(self.home),
                            'XDG_RUNTIME_DIR': f'/run/user/{uid}',
                            'DBUS_SESSION_BUS_ADDRESS': f'unix:path=/run/user/{uid}/bus'}
        self.versions_checked = set()

    @staticmethod
    def resource_name(resource_id):
        try:
            if str(uuid.UUID(resource_id)) == resource_id:
                return 'coolify-' + resource_id
        except (ValueError, TypeError, AttributeError):
            pass
        raise ValueError('Invalid native resource')

    def run(self, arguments, *, check=True, timeout=10, environment=None):
        return subprocess.run(arguments, env=environment or self.environment, cwd=self.home,
                              capture_output=True, text=True, check=check, timeout=timeout)

    def check_versions(self, policy):
        pins = (policy.get('generator_sha256'), policy.get('podman_version'), policy.get('native_adapter_sha256'))
        if pins in self.versions_checked:
            return
        if hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != pins[2]:
            raise ValueError('Pinned native adapter changed')
        if (hashlib.sha256(self.GENERATOR.read_bytes()).hexdigest() != pins[0]
                or hashlib.sha256(self.USER_GENERATOR.read_bytes()).hexdigest() != pins[0]):
            raise ValueError('Pinned generator changed')
        if self.run(['/usr/bin/podman', '--remote=false', '--version']).stdout.strip() != 'podman version ' + (pins[1] or ''):
            raise ValueError('Pinned Podman changed')
        self.versions_checked.add(pins)

    def protected_directory(self, path, private=True, create=False):
        if create:
            path.mkdir(mode=0o700, exist_ok=True)
        try:
            info = path.lstat()
        except FileNotFoundError as error:
            raise ValueError('Native directory missing') from error
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != self.uid
                or info.st_mode & (0o077 if private else 0o022)):
            raise ValueError('Unprotected native directory')

    def selection(self, resource_id):
        return self.home / '.config/containers/systemd' / self.resource_name(resource_id)

    def load(self, resource_id, bundle_hash, policy):
        self.resource_name(resource_id)
        if not isinstance(bundle_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', bundle_hash):
            raise ValueError('Invalid bundle hash')
        self.check_versions(policy)
        release = self.directory / 'releases' / resource_id / bundle_hash
        for path in [self.directory, release.parent.parent, release.parent, release]:
            self.protected_directory(path)
        descriptor = os.open(release / 'manifest.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'r', encoding='utf-8') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.uid or info.st_mode & 0o077
                    or info.st_nlink != 1 or info.st_size > 32768):
                raise ValueError('Unprotected release manifest')
            record = json.load(stream)
        if set(record) != {'metadata', 'spec'} or record['metadata']['bundle_hash'] != bundle_hash:
            raise ValueError('Release manifest identity changed')
        metadata = self.bundle_type(self.directory / 'releases', lambda *args: None).stage(record['spec'], resource_id, policy)
        if metadata != record['metadata']:
            raise ValueError('Release metadata changed')
        return {**record, 'path': release}

    @staticmethod
    def boot_id():
        value = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        if str(uuid.UUID(value)) != value:
            raise ValueError('Invalid boot identity')
        return value

    def pending_path(self, resource_id):
        self.resource_name(resource_id)
        return self.directory / ('pending-selection-' + resource_id + '.json')

    def pending(self, resource_id):
        path = self.pending_path(resource_id)
        if not path.exists() and not path.is_symlink():
            return None
        self.protected_directory(self.directory)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'r') as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != self.uid or info.st_mode & 0o077
                    or info.st_nlink != 1 or info.st_size > 2048):
                raise ValueError('Unprotected pending selection')
            record = json.load(stream)
        if set(record) != {'resource_id', 'operation_id', 'candidate_bundle_hash', 'previous_bundle_hash', 'boot_id'}:
            raise ValueError('Invalid pending selection fields')
        if record['resource_id'] != resource_id or str(uuid.UUID(record['operation_id'])) != record['operation_id']:
            raise ValueError('Pending selection scope mismatch')
        if str(uuid.UUID(record['boot_id'])) != record['boot_id']:
            raise ValueError('Invalid pending boot identity')
        for key in ['candidate_bundle_hash', 'previous_bundle_hash']:
            value = record[key]
            if value is None and key == 'previous_bundle_hash':
                continue
            if not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{64}', value):
                raise ValueError('Invalid pending bundle hash')
        return record

    def volatile_selection(self, resource_id):
        return self.runtime_directory / 'containers/systemd' / self.resource_name(resource_id)

    def active_selection(self, resource_id):
        volatile = self.volatile_selection(resource_id)
        return volatile if volatile.exists() or volatile.is_symlink() else self.selection(resource_id)

    def read_selection(self, selected, resource_id, policy):
        if selected == self.selection(resource_id):
            parents = [self.home, self.home / '.config', self.home / '.config/containers', selected.parent]
        else:
            parents = [self.runtime_directory, self.runtime_directory / 'containers', selected.parent]
        for path in parents:
            if path.exists() or path.is_symlink():
                self.protected_directory(path, private=False)
        if not selected.exists() and not selected.is_symlink():
            return None
        info = selected.lstat()
        if not stat.S_ISLNK(info.st_mode) or info.st_uid != self.uid:
            raise ValueError('Foreign selected path')
        target = Path(os.readlink(selected))
        expected_parent = self.directory / 'releases' / resource_id
        if target.parent != expected_parent or not re.fullmatch(r'[a-f0-9]{64}', target.name):
            raise ValueError('Foreign selected release')
        self.load(resource_id, target.name, policy)
        return target.name

    def current(self, resource_id, policy):
        pending = self.pending(resource_id)
        volatile = self.read_selection(self.volatile_selection(resource_id), resource_id, policy)
        durable = self.read_selection(self.selection(resource_id), resource_id, policy)
        if volatile and not pending:
            raise ValueError('Unjournaled volatile selection')
        if volatile and durable and volatile != durable:
            raise ValueError('Conflicting runtime and boot selections')
        current = volatile or durable
        if pending and current not in {None, pending['candidate_bundle_hash'], pending['previous_bundle_hash']}:
            raise ValueError('Selection is outside its pending operation')
        return current

    def write_pointer(self, selected, target):
        if selected == self.home / '.config/containers/systemd' / selected.name:
            parents = [self.home / '.config', self.home / '.config/containers', selected.parent]
        else:
            self.protected_directory(self.runtime_directory, private=False)
            parents = [self.runtime_directory / 'containers', selected.parent]
        for path in parents:
            self.protected_directory(path, private=False, create=True)
        temporary = selected.parent / ('.selection-' + str(uuid.uuid4()))
        try:
            temporary.symlink_to(target)
            os.replace(temporary, selected)
            self.bundle_type.sync_directory(selected.parent)
        finally:
            if temporary.is_symlink():
                temporary.unlink()

    def begin_update(self, resource_id, operation_id, candidate, previous, policy):
        if str(uuid.UUID(operation_id)) != operation_id:
            raise ValueError('Invalid selection operation')
        self.load(resource_id, candidate, policy)
        if previous:
            self.load(resource_id, previous, policy)
        record = self.pending(resource_id)
        expected = {'resource_id': resource_id, 'operation_id': operation_id,
                    'candidate_bundle_hash': candidate, 'previous_bundle_hash': previous}
        if record:
            if any(record[key] != value for key, value in expected.items()):
                raise ValueError('Selection belongs to a different operation')
        else:
            if self.current(resource_id, policy) != previous:
                raise ValueError('Previous release selection changed')
            record = {**expected, 'boot_id': self.boot_id()}
            descriptor, temporary = tempfile.mkstemp(prefix='.pending-', dir=self.directory)
            try:
                with os.fdopen(descriptor, 'w') as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(json.dumps(record, sort_keys=True, separators=(',', ':')) + '\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.pending_path(resource_id))
                self.bundle_type.sync_directory(self.directory)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
        durable = self.read_selection(self.selection(resource_id), resource_id, policy)
        if durable:
            if durable != previous:
                raise ValueError('Unexpected durable selection during publication')
            self.selection(resource_id).unlink()
            self.bundle_type.sync_directory(self.selection(resource_id).parent)

    def interrupted_update(self, resource_id, operation_id, policy):
        record = self.pending(resource_id)
        if not record or record['operation_id'] != operation_id:
            raise ValueError('Activation lacks its pending selection record')
        return self.current(resource_id, policy) is None and record['boot_id'] != self.boot_id()

    def select(self, resource_id, bundle_hash, policy):
        previous = self.current(resource_id, policy)
        pending = self.pending(resource_id)
        if pending and bundle_hash not in {None, pending['candidate_bundle_hash'], pending['previous_bundle_hash']}:
            raise ValueError('Selection is outside its pending operation')
        if bundle_hash is None:
            for selected in [self.selection(resource_id), self.volatile_selection(resource_id)]:
                if selected.is_symlink():
                    selected.unlink()
                    self.bundle_type.sync_directory(selected.parent)
            return
        selected = self.volatile_selection(resource_id) if pending else self.selection(resource_id)
        if previous == bundle_hash and selected.is_symlink():
            return
        target = self.load(resource_id, bundle_hash, policy)['path']
        self.write_pointer(selected, target)

    def commit(self, resource_id, bundle_hash, operation_id, policy):
        pending = self.pending(resource_id)
        if pending and (pending['operation_id'] != operation_id
                        or bundle_hash not in {None, pending['candidate_bundle_hash'], pending['previous_bundle_hash']}):
            raise ValueError('Commit belongs to a different operation')
        if self.current(resource_id, policy) != bundle_hash:
            raise ValueError('Cannot commit an unselected release')
        observed = self.inspect(resource_id, policy)
        if pending and bundle_hash == pending['candidate_bundle_hash'] and (not observed['active'] or not observed['health']):
            raise ValueError('Cannot boot-publish an unhealthy candidate')
        if bundle_hash:
            target = self.load(resource_id, bundle_hash, policy)['path']
            durable = self.selection(resource_id)
            if self.read_selection(durable, resource_id, policy) != bundle_hash:
                self.write_pointer(durable, target)
            volatile = self.volatile_selection(resource_id)
            if volatile.is_symlink():
                volatile.unlink()
                self.bundle_type.sync_directory(volatile.parent)
        self.reload_verify(resource_id, bundle_hash, policy)
        if pending:
            self.pending_path(resource_id).unlink()
            self.bundle_type.sync_directory(self.directory)
        return self.inspect(resource_id, policy)

    def quadlet_roots(self):
        return [self.runtime_directory / 'containers/systemd', self.home / '.config/containers/systemd',
                Path(f'/etc/containers/systemd/users/{self.uid}'), Path('/etc/containers/systemd/users')]

    def check_quadlet_sources(self, resource_id, files):
        selected = self.active_selection(resource_id)
        sources = set(files)
        dropins = set()
        for filename in files:
            path = Path(filename)
            dropins.add(path.suffix[1:] + '.d')
            dropins.add(filename + '.d')
            parts = path.stem.split('-')
            for count in range(1, len(parts)):
                dropins.add('-'.join(parts[:count]) + '-' + path.suffix + '.d')
        for root in self.quadlet_roots():
            if not root.exists():
                continue
            seen = set()
            for directory, folders, names in os.walk(root, followlinks=True):
                info = os.stat(directory)
                identity = (info.st_dev, info.st_ino)
                if identity in seen or len(seen) > 5000:
                    raise ValueError('Quadlet search tree is cyclic or unbounded')
                seen.add(identity)
                path = Path(directory)
                if path.name in dropins and any(name.endswith('.conf') for name in names):
                    raise ValueError('Effective Quadlet drop-ins require an explicit supported profile')
                for name in names:
                    if name in sources and path / name not in {self.selection(resource_id) / name, self.volatile_selection(resource_id) / name}:
                        raise ValueError('Foreign Quadlet source shadows managed resource')

    def check_systemd_sources(self, resource_id, files, selected):
        units = []
        for filename in files:
            path = Path(filename)
            suffix = {'container': '', 'network': '-network', 'volume': '-volume'}[path.suffix[1:]]
            units.append(path.stem + suffix + '.service')
        result = self.run(['/usr/bin/systemd-analyze', '--user', 'unit-paths'])
        roots = result.stdout.strip().split('\n')
        if len(roots) > 64 or any(not path.startswith('/') for path in roots):
            raise ValueError('Unsupported manager search paths')
        forbidden = {'service.d'}
        for unit in units:
            forbidden.update([unit + '.d', unit + '.wants', unit + '.requires'])
            stem = unit[:-len('.service')]
            parts = stem.split('-')
            for count in range(1, len(parts)):
                forbidden.add('-'.join(parts[:count]) + '-.service.d')
        generator = Path(f'/run/user/{self.uid}/systemd/generator')
        for directory in roots:
            root = Path(directory)
            if not root.is_dir():
                continue
            for name in forbidden:
                path = root / name
                if path.exists() or path.is_symlink():
                    raise ValueError('Effective systemd dependency or drop-in requires an explicit profile')
            for unit in units:
                path = root / unit
                if path.exists() or path.is_symlink():
                    if selected is None or root != generator or path.is_symlink():
                        raise ValueError('Foreign systemd source shadows managed unit')

    def show(self, unit):
        result = self.run(['/usr/bin/systemctl', '--user', 'show', unit,
                           '--property=ActiveState,SubState,InvocationID,LoadState,NeedDaemonReload,FragmentPath,DropInPaths,SourcePath,ExecStart,Description,Result'], check=False)
        if not result.stdout or result.returncode not in {0, 1}:
            raise ValueError('Systemd observation unavailable')
        return dict(line.split('=', 1) for line in result.stdout.split('\n') if '=' in line)

    def generated(self, resource_id, bundle_hash, policy):
        loaded = self.load(resource_id, bundle_hash, policy)
        files = self.bundle_type.compile(loaded['spec'], resource_id, policy)
        if self.current(resource_id, policy) != bundle_hash:
            raise ValueError('Cannot validate an unselected effective unit')
        self.check_quadlet_sources(resource_id, files)
        selected = self.active_selection(resource_id)
        with tempfile.TemporaryDirectory(prefix='.generator-', dir=self.directory) as temporary:
            result = self.run([str(self.USER_GENERATOR), '--user', temporary], timeout=30,
                              environment={**self.environment, 'QUADLET_UNIT_DIRS': str(selected)})
            units = {}
            for filename in files:
                path = Path(filename)
                suffix = {'container': '', 'network': '-network', 'volume': '-volume'}[path.suffix[1:]]
                unit = path.stem + suffix + '.service'
                units[unit] = (Path(temporary) / unit).read_bytes()
            return units

    def verify_effective(self, resource_id, bundle_hash, policy):
        for unit, content in self.generated(resource_id, bundle_hash, policy).items():
            properties = self.show(unit)
            fragment = Path(f'/run/user/{self.uid}/systemd/generator') / unit
            if (properties.get('LoadState') != 'loaded' or properties.get('NeedDaemonReload') != 'no'
                    or properties.get('DropInPaths') or properties.get('FragmentPath') != str(fragment)
                    or fragment.is_symlink() or fragment.read_bytes() != content):
                raise ValueError('Effective generated unit differs or has a foreign owner/override')

    @staticmethod
    def owned(labels, resource_id):
        if (not isinstance(labels, dict) or labels.get('coolify.managed') != 'true'
                or labels.get('coolify.resource') != resource_id
                or any(key.startswith(('io.containers.autoupdate', 'com.docker.compose.', 'io.podman.compose.')) for key in labels)):
            raise ValueError('Runtime object belongs to a foreign or second lifecycle owner')

    def object(self, kind, name):
        exists = self.run(['/usr/bin/podman', '--remote=false', kind, 'exists', name], check=False)
        if exists.returncode == 1:
            return None
        if exists.returncode != 0:
            raise ValueError('Runtime object observation unavailable')
        result = self.run(['/usr/bin/podman', '--remote=false', kind, 'inspect', name])
        records = json.loads(result.stdout)
        if not isinstance(records, list) or len(records) != 1:
            raise ValueError('Ambiguous runtime object')
        return records[0]

    def check_objects(self, resource_id, spec, selected):
        name = self.resource_name(resource_id)
        container = self.object('container', name)
        if container:
            self.owned(container['Config']['Labels'], resource_id)
            if selected is None or container['HostConfig']['RestartPolicy']['Name'] not in {'', 'no'}:
                raise ValueError('Container has no verified systemd owner')
            if container['Config']['Image'] != spec['image']:
                raise ValueError('Running container image differs from selected release')
            if container['Config']['Labels'].get('PODMAN_SYSTEMD_UNIT') != name + '.service':
                raise ValueError('Container lacks its systemd identity')
            expected_cgroup = f'/user.slice/user-{self.uid}.slice/user@{self.uid}.service/app.slice/{name}.service/libpod-payload-' + container['Id']
            if container.get('State', {}).get('Running') and container['State'].get('CgroupPath') != expected_cgroup:
                raise ValueError('Container is not in its owning systemd cgroup')
        network = self.object('network', name)
        if network:
            self.owned(network.get('labels'), resource_id)
            if network.get('internal') is not True or network.get('driver') != 'bridge':
                raise ValueError('Managed network semantics differ')
        for volume in spec['volumes']:
            observed = self.object('volume', name + '-' + volume['name'])
            if observed:
                self.owned(observed.get('Labels'), resource_id)
                if observed.get('Driver') != 'local' or observed.get('Options') not in [None, {}]:
                    raise ValueError('Volume uses an ungranted driver or host options')
        return container

    def inspect(self, resource_id, policy, bundle_hash=None):
        selected = self.current(resource_id, policy)
        if bundle_hash is not None and selected != bundle_hash:
            raise ValueError('Selected release changed')
        unit = self.resource_name(resource_id) + '.service'
        properties = self.show(unit)
        if selected is None:
            if properties.get('LoadState') not in {'not-found', ''} or properties.get('ActiveState') not in {'inactive', ''}:
                raise ValueError('Foreign unit exists without a selected release')
            return {'active': False, 'invocation': '', 'transitioning': False, 'failed': False,
                    'bundle_hash': None, 'health': False}
        loaded = self.load(resource_id, selected, policy)
        self.verify_effective(resource_id, selected, policy)
        container = self.check_objects(resource_id, loaded['spec'], selected)
        initial_invocation = properties.get('InvocationID', '')
        properties = self.show(unit)
        changed_invocation = initial_invocation != properties.get('InvocationID', '')
        state = properties.get('ActiveState')
        if state not in {'active', 'inactive', 'failed', 'activating', 'deactivating', 'reloading'}:
            raise ValueError('Unsupported manager state')
        active = not changed_invocation and state == 'active' and bool(container and container.get('State', {}).get('Running'))
        health = False
        if active:
            port = loaded['spec']['ports'][0]['host']
            check = loaded['spec']['health']
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=check['timeout_seconds'])
            try:
                connection.request('GET', check['path'])
                health = connection.getresponse().status == check['status']
            except (OSError, http.client.HTTPException):
                pass
            finally:
                connection.close()
        return {'active': active, 'invocation': properties.get('InvocationID', ''),
                'transitioning': changed_invocation or state in {'activating', 'deactivating', 'reloading'}, 'failed': state == 'failed',
                'bundle_hash': selected, 'health': health}

    def preflight(self, resource_id, bundle_hash, policy):
        loaded = self.load(resource_id, bundle_hash, policy)
        files = self.bundle_type.compile(loaded['spec'], resource_id, policy)
        selected = self.current(resource_id, policy)
        self.check_quadlet_sources(resource_id, files)
        if not Path('/sys/fs/cgroup/cgroup.controllers').is_file() or not Path('/var/lib/systemd/linger/' + self.home.name).is_file():
            raise ValueError('Rootless cgroup v2/lingering capability unavailable')
        self.check_systemd_sources(resource_id, files, selected)
        observed = self.inspect(resource_id, policy)
        selected_spec = self.load(resource_id, selected, policy)['spec'] if selected else loaded['spec']
        self.check_objects(resource_id, selected_spec, selected)
        if not observed['active'] and not observed['transitioning']:
            self.check_listener_available(loaded['spec']['ports'][0]['host'])
        return observed

    @staticmethod
    def check_listener_available(port):
        with socket.socket() as listener:
            # Closed backend connections can retain this port in TIME_WAIT.
            # SO_REUSEADDR allows those connections without sharing a live listener.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(('127.0.0.1', port))

    def reload_verify(self, resource_id, bundle_hash, policy):
        if self.current(resource_id, policy) != bundle_hash:
            raise ValueError('Cannot reload an unselected artifact')
        self.run(['/usr/bin/systemctl', '--user', 'daemon-reload'], timeout=30)
        if bundle_hash:
            self.verify_effective(resource_id, bundle_hash, policy)
        else:
            self.inspect(resource_id, policy)

    def image(self, resource_id, operation_id, spec, policy):
        self.resource_name(resource_id)
        if str(uuid.UUID(operation_id)) != operation_id:
            raise ValueError('Invalid image operation')
        image = spec['image']
        if self.object('image', image):
            return {'ready': True, 'failed': False}
        unit = 'coolify-pull-' + operation_id + '.service'
        fingerprint = hashlib.sha256((resource_id + '\n' + image).encode()).hexdigest()
        description = 'Coolify image acquisition ' + fingerprint
        observed = self.show(unit)
        if observed.get('LoadState') == 'not-found':
            self.run(['/usr/bin/systemd-run', '--user', '--no-block', '--unit=' + unit,
                      '--description=' + description, '--property=Type=oneshot', '--property=RemainAfterExit=yes',
                      '--property=TimeoutStartSec=300', '/usr/bin/podman', '--remote=false', 'pull', image])
            return {'ready': False, 'failed': False}
        if observed.get('Description') != description or image not in observed.get('ExecStart', ''):
            raise ValueError('Foreign image acquisition unit')
        return {'ready': False, 'failed': observed.get('ActiveState') in {'failed', 'inactive'}}

    def stop(self, resource_id, policy):
        self.inspect(resource_id, policy)
        self.run(['/usr/bin/systemctl', '--user', '--no-block', 'stop', self.resource_name(resource_id) + '.service'])

    def start(self, resource_id, policy, before_request=None):
        observed = self.inspect(resource_id, policy)
        if observed['bundle_hash'] is None:
            raise ValueError('No selected release to start')
        self.preflight(resource_id, observed['bundle_hash'], policy)
        if before_request:
            before_request()
        self.run(['/usr/bin/systemctl', '--user', '--no-block', 'start', self.resource_name(resource_id) + '.service'])
