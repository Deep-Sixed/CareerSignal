from concurrent.futures import ThreadPoolExecutor

import pytest

from data import store


@pytest.mark.parametrize(
    ("version", "fixed"),
    [
        ((3, 38, 0), False),
        ((3, 44, 5), False),
        ((3, 44, 6), True),
        ((3, 44, 99), True),
        ((3, 45, 0), False),
        ((3, 45, 1), False),
        ((3, 50, 6), False),
        ((3, 50, 7), True),
        ((3, 50, 99), True),
        ((3, 51, 0), False),
        ((3, 51, 1), False),
        ((3, 51, 2), False),
        ((3, 51, 3), True),
        ((3, 51, 4), True),
        ((3, 53, 4), True),
        ((4, 0, 0), True),
    ],
)
def test_wal_reset_fix_ranges(version, fixed):
    assert store.has_wal_reset_fix(version) is fixed


@pytest.mark.parametrize(
    ("version", "nearest"),
    [
        ((3, 44, 5), (3, 44, 6)),
        ((3, 45, 1), (3, 51, 3)),
        ((3, 50, 4), (3, 50, 7)),
        ((3, 51, 2), (3, 51, 3)),
        ((3, 53, 4), (3, 53, 4)),
    ],
)
def test_nearest_wal_reset_fix(version, nearest):
    assert store.nearest_wal_reset_fix(version) == nearest


def test_runtime_report_requires_no_database(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    report = store.sqlite_report()
    assert report["sqlite"]
    assert report["minimum"] == "3.38.0"
    assert isinstance(report["wal_reset_fix"], bool)
    assert not (tmp_path / "var").exists()


def test_runtime_floor_refuses_before_touching_filesystem(tmp_path, monkeypatch):
    path = tmp_path / "nested" / "db.sqlite"
    monkeypatch.setattr(store.sqlite3, "sqlite_version_info", (3, 37, 2))
    with pytest.raises(RuntimeError, match=r"SQLite 3\.37\.2 found; 3\.38\.0 or newer required"):
        with store.connection(path):
            pass
    assert not path.exists()
    assert not path.parent.exists()


def test_sqlite_thread_affinity_guard_remains_enabled(tmp_path):
    store.migrate(tmp_path / "db")
    with store.connection(tmp_path / "db") as conn, ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(conn.execute, "SELECT 1")
        with pytest.raises(store.sqlite3.ProgrammingError, match="same thread"):
            future.result(timeout=2)
