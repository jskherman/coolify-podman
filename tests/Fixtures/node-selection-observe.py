#!/usr/bin/env python3
"""Read bounded disposable selection, journal and data witnesses; no mutation."""
import json
import os
from pathlib import Path
import pwd
import sqlite3
import subprocess
import sys
import uuid

fixture, resource_id = sys.argv[1:]
assert os.getuid() == 0 and str(uuid.UUID(resource_id)) == resource_id
assert Path('/etc/coolify-disposable-fixture').read_text().strip() == fixture
name = 'coolify-' + resource_id
policy = json.loads(Path('/etc/coolify-node/policies/1001.json').read_text())
assert policy['resources'][resource_id]['kind'] == 'quadlet'
state = {'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
         'persistent': Path('/home/workload/.config/containers/systemd/' + name).is_symlink(),
         'volatile': Path('/run/user/1001/containers/systemd/' + name).is_symlink()}
marker = Path('/var/lib/coolify-node/1001/pending-selection-' + resource_id + '.json')
state['pending_selection'] = json.loads(marker.read_text()) if marker.exists() else None
with sqlite3.connect('file:/var/lib/coolify-node/1001/journal.sqlite?mode=ro', uri=True) as db:
    state['generation'] = db.execute('select generation from resources where id=?', (resource_id,)).fetchone()[0]
    state['operations'] = db.execute("select id,state,before_state from operations where resource_id=? and state in ('executing','needs_intervention')", (resource_id,)).fetchall()
os.setgroups([])
os.setgid(1001)
os.setuid(1001)
os.chdir('/home/workload')
environment = {'PATH': '/usr/bin:/bin', 'HOME': '/home/workload', 'XDG_RUNTIME_DIR': '/run/user/1001',
               'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/run/user/1001/bus'}
result = subprocess.run(['/usr/bin/podman', '--remote=false', 'volume', 'inspect', name + '-data'],
                        env=environment, capture_output=True, text=True, timeout=10, check=True)
volume = json.loads(result.stdout)[0]
assert volume['Labels']['coolify.resource'] == resource_id
witness = Path(volume['Mountpoint']) / 'writer.log'
state['writer_log'] = witness.read_text()[:4096] if witness.is_file() else ''
print(json.dumps(state))
