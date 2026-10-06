from concurrent.futures import ThreadPoolExecutor
import sqlite3
from threading import Barrier, BrokenBarrierError
from types import SimpleNamespace

import httpx
import pytest

from archon_horizon.pipeline.client import AgentClient


@pytest.mark.parametrize('legacy', [False, True])
def test_parallel_client_startup_preserves_and_migrates_journal(tmp_path, monkeypatch, legacy):
    path = tmp_path / 'api-intents.sqlite3'
    connect = sqlite3.connect
    if legacy:
        with connect(path) as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE intent (id TEXT PRIMARY KEY, execution_id TEXT NOT NULL, '
                'fingerprint TEXT NOT NULL, method TEXT NOT NULL, path TEXT NOT NULL, body TEXT NOT NULL, '
                'status TEXT NOT NULL, response TEXT, created_at REAL NOT NULL)')
            db.execute("INSERT INTO intent VALUES ('retained', 'previous', 'fingerprint', 'POST', "
                "'/api/v3/commands', '{}', 'pending', NULL, 1)")

    startup = Barrier(2)
    schema_read = Barrier(2)

    class PausedSchemaConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            cursor = super().execute(sql, *args, **kwargs)
            if sql == 'PRAGMA table_info(intent)':
                rows = cursor.fetchall()
                # Without an enclosing write transaction, both clients observe
                # the old schema before either migrates. A serialized second
                # client cannot reach this read until the first has committed.
                try:
                    schema_read.wait(timeout=1)
                except BrokenBarrierError:
                    pass
                return SimpleNamespace(fetchall=lambda: rows)
            return cursor

    monkeypatch.setattr(sqlite3, 'connect', lambda *args, **kwargs:
        connect(*args, factory=PausedSchemaConnection, **kwargs))

    def start(number):
        with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={'ok': True}))) as transport:
            startup.wait(timeout=5)
            client = AgentClient('https://horizon.invalid', 'token', f'execution-{number}',
                tmp_path, client=transport, assignment_id='same-owner')
            assert client.request('GET', '/api/v3/records/assignment') == {'ok': True}
            return client.pending()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(start, range(2)))
    assert results[0] == results[1]
    if legacy:
        assert results[0][0]['id'] == 'retained' and results[0][0]['status'] == 'pending'
    else:
        assert results[0] == []
    with connect(path) as db:
        fields = {row[1] for row in db.execute('PRAGMA table_info(intent)')}
        assert {'attempts', 'retry_at', 'resolution'} <= fields
        if legacy:
            assert db.execute('SELECT attempts,retry_at,resolution FROM intent').fetchone() == (0, None, None)
        assert db.execute("SELECT value FROM metadata WHERE key='assignment_id'").fetchone() == ('same-owner',)
