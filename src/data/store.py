"""Connection ownership, bounded writer reservation and atomic migrations."""

import math
import os
import sqlite3
import time
from contextlib import contextmanager
from hashlib import sha256
from importlib import resources
from pathlib import Path

# Migration 0001 uses unixepoch(), added by SQLite 3.38.0. Check this before touching the
# filesystem so an unsupported interpreter cannot leave behind a half-initialized path.
MIN_SQLITE = (3, 38, 0)
# SQLite's WAL-reset race is fixed on the two backport branches below and from 3.51.3
# onward. Keep these as ranges: 3.45.x is *not* covered by the 3.44.6 backport.
WAL_RESET_FIXED_344 = ((3, 44, 6), (3, 45, 0))
WAL_RESET_FIXED_350 = ((3, 50, 7), (3, 51, 0))
WAL_RESET_FIXED_MAIN = (3, 51, 3)

# Writers keep a short reservation so transaction() owns the retry policy. Journal-mode
# negotiation is a startup step and needs its own budget: changing it takes an exclusive
# lock that concurrent first starts genuinely contend for.
BUSY_TIMEOUT_MS = 20
STARTUP_BUSY_TIMEOUT_MS = 250
STARTUP_TIMEOUT = 5.0


def database_path(path=None) -> Path:
    return (
        Path(path or os.getenv("CAREERSIGNAL_DB_PATH") or "var/careersignal.db")
        .expanduser()
        .resolve()
    )


def sqlite_version() -> tuple[int, int, int]:
    """The SQLite runtime carried by this Python interpreter."""
    return tuple(sqlite3.sqlite_version_info[:3])


def has_wal_reset_fix(version=None) -> bool:
    """Whether this SQLite version contains the WAL-reset race fix."""
    value = sqlite_version() if version is None else tuple(version)
    return (
        WAL_RESET_FIXED_344[0] <= value < WAL_RESET_FIXED_344[1]
        or WAL_RESET_FIXED_350[0] <= value < WAL_RESET_FIXED_350[1]
        or value >= WAL_RESET_FIXED_MAIN
    )


def nearest_wal_reset_fix(version=None) -> tuple[int, int, int]:
    """The nearest patched release an unpatched branch can move to."""
    value = sqlite_version() if version is None else tuple(version)
    if has_wal_reset_fix(value):
        return value
    if (3, 44, 0) <= value < (3, 45, 0):
        return (3, 44, 6)
    if (3, 50, 0) <= value < (3, 51, 0):
        return (3, 50, 7)
    return WAL_RESET_FIXED_MAIN


def sqlite_report() -> dict:
    """Machine-readable runtime facts that require no database file."""
    version = sqlite_version()
    return {
        "sqlite": ".".join(map(str, version)),
        "minimum": ".".join(map(str, MIN_SQLITE)),
        "wal_reset_fix": has_wal_reset_fix(version),
        "nearest_wal_reset_fix": ".".join(map(str, nearest_wal_reset_fix(version))),
    }


def _require_sqlite_runtime() -> None:
    version = sqlite_version()
    if version < MIN_SQLITE:
        found = ".".join(map(str, version))
        required = ".".join(map(str, MIN_SQLITE))
        raise RuntimeError(f"SQLite {found} found; {required} or newer required")


def _contended(exc) -> bool:
    return any(s in str(exc).lower() for s in ("locked", "busy"))


def _negotiate_wal(conn, timeout):
    """Wait out other initializers instead of failing the first start of a fresh database."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0].lower()
            if mode == "wal":
                return
            detail = f"journal mode is {mode}"
        except Exception as exc:
            if not _contended(exc):
                raise
            detail = str(exc)
        if time.monotonic() >= deadline:
            raise RuntimeError(f"WAL unavailable: {detail}")
        time.sleep(min(0.01, max(0, deadline - time.monotonic())))


@contextmanager
def connection(path=None):
    _require_sqlite_runtime()
    target = database_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Keep sqlite3's default check_same_thread=True. Each application operation owns its
    # connection on the calling thread; the guard is useful once the web surface introduces
    # real worker threads and must not be weakened for a test helper.
    conn = sqlite3.connect(str(target), isolation_level=None)
    try:
        conn.execute(f"PRAGMA busy_timeout={STARTUP_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        if conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("Foreign keys unavailable")
        _negotiate_wal(conn, STARTUP_TIMEOUT)
        # Restore the short reservation before the caller can open a transaction.
        conn.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn, timeout=1.0):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Timeout must be positive and finite")
    if conn.in_transaction:
        raise RuntimeError("Nested transactions are not supported")
    deadline = time.monotonic() + timeout
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")
            break
        except Exception as exc:
            if not _contended(exc):
                raise
            if time.monotonic() >= deadline:
                raise TimeoutError("Write reservation exhausted") from exc
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


def migration_files():
    return sorted(
        (p for p in resources.files("data.migrations").iterdir() if p.name.endswith(".sql")),
        key=lambda p: p.name,
    )


def migrate(path=None):
    applied = []
    with connection(path) as conn, transaction(conn):
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations "
            "(version TEXT PRIMARY KEY, digest TEXT NOT NULL)"
        )
        existing = dict(conn.execute("SELECT version,digest FROM schema_migrations").fetchall())
        files = migration_files()
        if set(existing) - {p.name for p in files}:
            raise RuntimeError("Database has unknown migrations")
        for resource in files:
            sql = resource.read_text(encoding="utf-8")
            digest = sha256(sql.encode()).hexdigest()
            if resource.name in existing:
                if existing[resource.name] != digest:
                    raise RuntimeError("Applied migration was modified")
                continue
            for statement in sql.split("-- statement"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute("INSERT INTO schema_migrations VALUES (?,?)", (resource.name, digest))
            applied.append(resource.name)
    return applied


def verify_contract(path):
    if not database_path(path).is_file():
        raise RuntimeError("Database does not exist")
    with connection(path) as conn:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("Integrity check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise RuntimeError("Foreign key check failed")
        expected = {
            p.name: sha256(p.read_text(encoding="utf-8").encode()).hexdigest()
            for p in migration_files()
        }
        if (
            dict(conn.execute("SELECT version,digest FROM schema_migrations").fetchall())
            != expected
        ):
            raise RuntimeError("Migration contract incomplete")
