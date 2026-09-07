import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from data import store


def test_migration_repeat_contract_and_rollback(tmp_path):
    path = tmp_path / "db"
    assert store.migrate(path) == ["0001_baseline.sql"]
    assert store.migrate(path) == []
    store.verify_contract(path)
    with store.connection(path) as conn:
        with pytest.raises(RuntimeError), store.transaction(conn):
            conn.execute("INSERT INTO messages VALUES ('rollback','digest')")
            raise RuntimeError("abort")
        assert conn.execute("SELECT * FROM messages").fetchall() == []
        with store.transaction(conn):
            conn.execute("INSERT INTO messages VALUES ('commit','digest')")
        assert len(conn.execute("SELECT * FROM messages").fetchall()) == 1
        with pytest.raises(Exception), store.transaction(conn):
            conn.execute("INSERT INTO decisions VALUES ('missing',1,'operator')")


def test_failed_migration_is_atomic_and_upgrade_works(tmp_path, monkeypatch):
    path = tmp_path / "db"
    store.migrate(path)
    original = store.migration_files()
    upgrade = tmp_path / "0002_upgrade.sql"
    upgrade.write_text("CREATE TABLE upgrade_marker(id INTEGER);\n-- statement\nINVALID SQL")
    monkeypatch.setattr(store, "migration_files", lambda: original + [upgrade])
    with pytest.raises(Exception):
        store.migrate(path)
    with store.connection(path) as conn:
        assert not conn.execute(
            "SELECT name FROM sqlite_master WHERE name='upgrade_marker'"
        ).fetchall()
        assert len(conn.execute("SELECT * FROM schema_migrations").fetchall()) == 1
    upgrade.write_text("CREATE TABLE upgrade_marker(id INTEGER)")
    assert store.migrate(path) == [upgrade.name]
    store.verify_contract(path)
    upgrade.write_text("CREATE TABLE changed(id INTEGER)")
    with pytest.raises(RuntimeError, match="modified"):
        store.migrate(path)


def test_real_two_connection_retry_success_and_exhaustion(tmp_path):
    path = tmp_path / "db"
    store.migrate(path)
    ready = Event()

    def writer():
        with store.connection(path) as conn:
            ready.set()
            with store.transaction(conn):
                conn.execute("INSERT INTO messages VALUES ('second','digest')")

    with store.connection(path) as first, store.connection(path) as second:
        with store.transaction(first):
            started = time.monotonic()
            with pytest.raises(TimeoutError), store.transaction(second, timeout=0.06):
                pass
            assert time.monotonic() - started < 0.5
        with ThreadPoolExecutor() as pool:
            with store.transaction(first):
                future = pool.submit(writer)
                assert ready.wait(2)
                time.sleep(0.08)
                assert not future.done()
            future.result(timeout=3)
        assert first.execute("SELECT id FROM messages").fetchall() == [("second",)]


def test_concurrent_initializers(tmp_path):
    path = tmp_path / "db"
    # Configure WAL before racing the migration transactions.
    with store.connection(path):
        pass
    with ThreadPoolExecutor() as pool:
        results = list(pool.map(lambda _: store.migrate(path), range(2)))
    assert sorted(len(r) for r in results) == [0, 1]


def test_audit_cannot_be_mutated(tmp_path):
    path = tmp_path / "db"
    store.migrate(path)
    with store.connection(path) as conn:
        conn.execute("INSERT INTO audit(event) VALUES ('test')")
        for sql in ("DELETE FROM audit", "UPDATE audit SET event='changed'"):
            with pytest.raises(Exception):
                conn.execute(sql)
