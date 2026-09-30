#!/usr/bin/env python3
"""Read-only identity probe, executed as the configured SSH user without sudo.

The output is observation, never authorization or a deployment-support claim.
No command, path, unit or environment override is accepted from stdin/arguments.
"""
import json
import os
from pathlib import Path
import pwd
import subprocess


def observe(arguments):
    try:
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=20,
                                env={**os.environ, 'LC_ALL': 'C'})
        if result.returncode == 0 and len(result.stdout) <= 262144:
            return result.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def main():
    uid, gid = os.getuid(), os.getgid()
    home = pwd.getpwuid(uid).pw_dir
    os.chdir(home)
    # Bind observations to this identity's local manager, never a remote Podman connection.
    os.environ.pop('CONTAINER_HOST', None)
    os.environ.pop('CONTAINER_CONNECTION', None)
    os.environ['XDG_RUNTIME_DIR'] = f'/run/user/{uid}'
    os.environ['DBUS_SESSION_BUS_ADDRESS'] = f'unix:path=/run/user/{uid}/bus'
    raw = observe(['podman', '--remote=false', 'info', '--format', 'json'])
    try:
        info = json.loads(raw) if raw else {}
    except ValueError:
        info = {}
    host = info.get('host', {})
    store = info.get('store', {})
    version = info.get('version', {}).get('Version')
    systemd = observe(['systemctl', '--version'])
    user_manager = observe(['systemctl', '--user', 'show', '--property=Version', '--value'])
    linger = observe(['loginctl', 'show-user', str(uid), '--property=Linger', '--value'])
    generator = Path('/usr/lib/systemd/system-generators/podman-system-generator')
    restic = observe(['restic', 'version'])
    buildah = observe(['buildah', '--version'])
    rclone = observe(['rclone', 'version'])
    capabilities = {}

    def capability(name, available, remediation):
        capabilities[name] = {'available': bool(available), 'remediation': None if available else remediation}

    capability('runtime.podman', version, 'Install Podman and verify local podman info as this SSH identity.')
    capability('network.rootless', uid != 0 and host.get('security', {}).get('rootless') is True,
               'Use a dedicated non-root SSH identity with subordinate UID/GID ranges.')
    capability('systemd.user_units', user_manager, 'Enable and start the systemd user manager for this identity.')
    capability('systemd.linger', linger == 'yes', 'An administrator must enable lingering for this identity.')
    capability('systemd.quadlet_generator', generator.is_file() and os.access(generator, os.X_OK),
               'Install the Podman Quadlet generator. Bundle validation is a separate deployment gate.')
    capability('runtime.cgroup_v2', host.get('cgroupVersion') == 'v2' and host.get('cgroupManager') == 'systemd',
               'Configure cgroup v2 and the systemd cgroup manager for this identity.')
    capability('build.buildah', buildah, 'Install Buildah under the isolated build profile before enabling builds.')
    capability('backup.restic', restic, 'Install Restic for the backup identity before enabling backups.')
    capability('backup.rclone', rclone, 'Install rclone only when the chosen backup transport requires it.')
    capability('compose.pinned_provider', False, 'No Compose provider profile has been qualified; direct Compose is unavailable.')
    print(json.dumps({
        'schema_version': 1, 'runtime': 'podman', 'uid': uid, 'gid': gid,
        'manager': 'user' if uid else 'system',
        'storage': {'graph_root': store.get('graphRoot'), 'driver': store.get('graphDriverName')},
        'versions': {'podman': version, 'systemd': systemd.splitlines()[0] if systemd else None,
                     'restic': restic, 'buildah': buildah, 'rclone': rclone.splitlines()[0] if rclone else None},
        'capabilities': capabilities,
    }, sort_keys=True))


if __name__ == '__main__':
    main()
