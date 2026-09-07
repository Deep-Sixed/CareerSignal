import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import libsql
import pytest

from data import store

# Independent processes, released together, with no pre-warmed journal mode: the state a
# real first run starts from. The two tests above pin the behaviour deterministically;
# this one proves it end to end across process boundaries.
INITIALIZER = """
import json, os, sys, time
from pathlib import Path

path, gate, go = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
from data import store

# Import and register first, then spin on one stat() without yielding. The contended
# window is sub-millisecond, so anything slower lets the workers file past one another.
(gate / str(os.getpid())).write_text("ready", encoding="utf-8")
limit = time.monotonic() + 30
while not go.exists() and time.monotonic() < limit:
    pass

try:
    applied = store.migrate(path)
    store.verify_contract(path)
    with store.connection(path) as conn:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
    print(json.dumps({"applied": applied, "journal_mode": mode}))
except Exception as exc:
    print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}))
"""


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
    with ThreadPoolExecutor() as pool:
        results = list(pool.map(lambda _: store.migrate(path), range(2)))
    assert sorted(len(r) for r in results) == [0, len(store.migration_files())]


def test_first_start_waits_out_a_contended_journal_mode(tmp_path):
    """A fresh database is not yet in WAL, so switching it can genuinely be locked out."""
    path = tmp_path / "contended.db"
    holder = libsql.connect(str(path), isolation_level=None)
    holder.execute("CREATE TABLE placeholder (id INTEGER)")
    assert holder.execute("PRAGMA journal_mode").fetchone()[0].lower() != "wal"
    holder.execute("BEGIN EXCLUSIVE")
    released = Event()

    def release():
        time.sleep(0.3)
        holder.rollback()
        released.set()

    # Timed from before the releaser starts, so a slow scheduler cannot shorten the wait.
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(release)
        with store.connection(path) as conn:
            waited = time.monotonic() - started
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            # The reservation policy transaction() relies on is restored for the caller.
            assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == store.BUSY_TIMEOUT_MS
    assert released.is_set() and 0.3 <= waited < store.STARTUP_TIMEOUT
    holder.close()


def test_contended_journal_mode_gives_up_within_its_budget(tmp_path, monkeypatch):
    """Contention that never clears must still fail, not hang."""
    monkeypatch.setattr(store, "STARTUP_TIMEOUT", 0.3)
    path = tmp_path / "blocked.db"
    holder = libsql.connect(str(path), isolation_level=None)
    holder.execute("CREATE TABLE placeholder (id INTEGER)")
    holder.execute("BEGIN EXCLUSIVE")
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="WAL unavailable"):
        with store.connection(path):
            pass
    assert 0.3 <= time.monotonic() - started < 3
    holder.rollback()
    holder.close()


def test_concurrent_first_start_of_a_fresh_database(tmp_path):
    """Separate processes reaching an empty path together must all initialize."""
    script = tmp_path / "initializer.py"
    script.write_text(INITIALIZER, encoding="utf-8")
    database, gate, go = tmp_path / "fresh.db", tmp_path / "gate", tmp_path / "go"
    workers = 6
    gate.mkdir()

    def start(_):
        return subprocess.run(
            [sys.executable, str(script), str(database), str(gate), str(go)],
            capture_output=True,
            text=True,
            timeout=120,
        )

    def release():
        limit = time.monotonic() + 60
        while len(list(gate.iterdir())) < workers and time.monotonic() < limit:
            time.sleep(0.001)
        go.write_text("go", encoding="utf-8")

    with ThreadPoolExecutor(max_workers=workers + 1) as pool:
        releaser = pool.submit(release)
        finished = list(pool.map(start, range(workers)))
        releaser.result(timeout=90)
    for done in finished:
        assert done.returncode == 0, done.stderr
    results = [json.loads(done.stdout) for done in finished]

    assert [r for r in results if "error" not in r] == results
    assert {r["journal_mode"] for r in results} == {"wal"}
    # Every worker observes the schema; exactly one is the writer that applied it.
    applied = sorted(len(r["applied"]) for r in results)
    assert applied == [0] * (workers - 1) + [len(store.migration_files())]


def test_audit_cannot_be_mutated(tmp_path):
    path = tmp_path / "db"
    store.migrate(path)
    with store.connection(path) as conn:
        conn.execute("INSERT INTO audit(event) VALUES ('test')")
        for sql in ("DELETE FROM audit", "UPDATE audit SET event='changed'"):
            with pytest.raises(Exception):
                conn.execute(sql)
