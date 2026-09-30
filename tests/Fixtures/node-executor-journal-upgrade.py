#!/usr/bin/env python3
"""Migrate only this disposable prototype's recorded journal; never production data."""
import fcntl
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

fixture = sys.argv[1]
assert Path('/etc/coolify-disposable-fixture').read_text().strip() == fixture
assert Path('/etc/coolify-node/fixture-owner').read_text() == fixture
requests = json.loads(Path('/root/node-operation-manifest.json').read_text())
canonical = lambda value: json.dumps(value, sort_keys=True, separators=(',', ':'))
with open('/var/lib/coolify-node/1001/executor.lock', 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    with sqlite3.connect('/var/lib/coolify-node/1001/journal.sqlite') as database:
        database.row_factory = sqlite3.Row
        assert database.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        generations = [tuple(row) for row in database.execute('SELECT * FROM resources')]
        assert len(generations) == 1 and generations[0][1] == 5
        rows = database.execute('SELECT * FROM operations').fetchall()
        for row in rows:
            assert row['state'] == 'succeeded', 'Refusing migration of unresolved effects'
            request = requests[row['id']]
            assert hashlib.sha256(canonical(['fixture-controller', request]).encode()).hexdigest() == row['payload_hash']
        columns = [row[1] for row in database.execute('PRAGMA table_info(operations)')]
        if 'request' not in columns:
            backup = Path('/root/node-journal-schema-before.sqlite')
            assert not backup.exists(), 'Reconcile previous schema upgrade before retrying'
            with sqlite3.connect(backup) as snapshot:
                database.backup(snapshot)
            with database:
                database.execute('ALTER TABLE operations ADD COLUMN principal TEXT')
                database.execute('ALTER TABLE operations ADD COLUMN request TEXT')
                for row in rows:
                    database.execute('UPDATE operations SET principal=?,request=? WHERE id=?',
                                     ('fixture-controller', canonical(requests[row['id']]), row['id']))
                database.execute('CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)')
                database.execute("INSERT OR REPLACE INTO metadata VALUES ('schema_version','2')")
        assert [tuple(row) for row in database.execute('SELECT * FROM resources')] == generations
        assert database.execute("SELECT value FROM metadata WHERE name='schema_version'").fetchone()[0] == '2'
        for row in database.execute('SELECT id,principal,request FROM operations'):
            assert row['principal'] == 'fixture-controller' and json.loads(row['request']) == requests[row['id']]
print('schema_2_verified_generation_5_preserved')
