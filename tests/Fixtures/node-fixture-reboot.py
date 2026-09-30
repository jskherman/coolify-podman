#!/usr/bin/env python3
"""One recorded kernel reboot of an explicitly disposable fixture."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

fixture, operation_id, expected_boot = sys.argv[1:]
assert os.getuid() == 0 and sys.flags.isolated
assert Path('/etc/coolify-disposable-fixture').read_text().strip() == fixture
assert Path('/etc/coolify-node/fixture-owner').read_text() == fixture
assert str(uuid.UUID(operation_id)) == operation_id
assert str(uuid.UUID(expected_boot)) == expected_boot
path = Path('/etc/coolify-node/reboot-' + operation_id + '.json')
with open('/etc/coolify-node/reboot.lock', 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    if path.exists():
        record = json.loads(path.read_text())
        assert record == {'fixture': fixture, 'operation_id': operation_id, 'boot_id': expected_boot, 'state': 'requested'}
        print(json.dumps(record), flush=True)
    else:
        assert Path('/proc/sys/kernel/random/boot_id').read_text().strip() == expected_boot
        record = {'fixture': fixture, 'operation_id': operation_id, 'boot_id': expected_boot, 'state': 'requested'}
        with path.open('x') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        descriptor = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        print(json.dumps(record), flush=True)
        subprocess.run(['/usr/bin/systemctl', 'reboot'], check=True, timeout=15)
