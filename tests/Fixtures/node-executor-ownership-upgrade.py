#!/usr/bin/env python3
"""Recorded compatible code publication; no generation or lifecycle mutation."""
import fcntl,hashlib,json,os,sqlite3,sys
from pathlib import Path
fixture,executor_hash,adapter_hash=sys.argv[1:]
assert os.getuid()==0 and Path('/etc/coolify-disposable-fixture').read_text().strip()==fixture
assert Path('/etc/coolify-node/fixture-owner').read_text()==fixture
sources={name:Path('/root/'+name).read_bytes() for name in ['coolify-node-executor.py','coolify-node-native.py']}
assert hashlib.sha256(sources['coolify-node-executor.py']).hexdigest()==executor_hash
assert hashlib.sha256(sources['coolify-node-native.py']).hexdigest()==adapter_hash
policy_file=Path('/etc/coolify-node/policies/1001.json');record_file=Path('/etc/coolify-node/native-ownership-upgrade.json')
def save(path,data,mode):
 temporary=path.with_suffix('.tmp')
 with temporary.open('wb') as f:
  os.fchmod(f.fileno(),mode);f.write(data);f.flush();os.fsync(f.fileno())
 os.replace(temporary,path)
 fd=os.open(path.parent,os.O_DIRECTORY)
 try:os.fsync(fd)
 finally:os.close(fd)
with open('/var/lib/coolify-node/1001/executor.lock','a') as lock:
 fcntl.flock(lock,fcntl.LOCK_EX);policy=json.loads(policy_file.read_text())
 with sqlite3.connect('file:/var/lib/coolify-node/1001/journal.sqlite?mode=ro',uri=True) as db:
  if record_file.exists():
   record=json.loads(record_file.read_text());assert policy in [record['old_policy'],record['new_policy']]
   assert record['fixture']==fixture and record['executor_hash']==executor_hash and record['adapter_hash']==adapter_hash
  else:
   assert policy['version']==8 and policy['controller_epoch']==2
   new=json.loads(json.dumps(policy));new['version']=9
   for resource,grant in new['resources'].items():
    if grant.get('kind')=='quadlet':
     assert hashlib.sha256(Path('/usr/local/libexec/coolify-node-native.py').read_bytes()).hexdigest()==grant['native_adapter_sha256']
     grant['native_adapter_sha256']=adapter_hash
   record={'fixture':fixture,'state':'prepared','old_policy':policy,'new_policy':new,'executor_hash':executor_hash,'adapter_hash':adapter_hash,
           'operations':db.execute("select id,payload_hash,state from operations where state in ('executing','needs_intervention')").fetchall(),
           'generations':db.execute('select id,generation from resources').fetchall(),
           'reason':'Use observed Podman systemd label and actual cgroup; keep original activation/invocation.'}
   save(record_file,json.dumps(record).encode(),0o600)
  assert db.execute('select id,generation from resources').fetchall()==[tuple(x) for x in record['generations']]
  for name,data in sources.items():save(Path('/usr/local/libexec/'+name),data,0o755)
  save(policy_file,json.dumps(record['new_policy']).encode(),0o644)
  record['state']='published';save(record_file,json.dumps(record).encode(),0o600)
print(json.dumps({'state':record['state'],'policy_version':9,'pending':record['operations']}))
