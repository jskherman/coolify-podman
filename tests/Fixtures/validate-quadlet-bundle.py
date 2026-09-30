#!/usr/bin/env python3
"""Generator-only validation inside the disposable workload identity; no activation."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

request = json.load(sys.stdin)
assert os.getuid() == 1001 and os.getgid() == 1001
assert Path('/etc/coolify-disposable-fixture').read_text().strip() == request['fixture']
assert len(request['files']) <= 10
with tempfile.TemporaryDirectory(prefix='coolify-quadlet-validate-', dir='/home/workload') as directory:
    for name, content in request['files'].items():
        assert re.fullmatch(r'coolify-[a-z0-9-]+\.(container|network|volume)', name)
        assert len(content) < 16384
        path = Path(directory) / name
        path.write_text(content)
        path.chmod(0o600)
    result = subprocess.run(['/usr/lib/systemd/system-generators/podman-system-generator', '--user', '--dryrun'],
        env={'PATH': '/usr/bin:/bin', 'HOME': '/home/workload', 'XDG_RUNTIME_DIR': '/run/user/1001',
             'QUADLET_UNIT_DIRS': directory, 'LC_ALL': 'C'}, capture_output=True, text=True, timeout=30)
    print(json.dumps({'code': result.returncode, 'output': result.stdout, 'errors': result.stderr}))
