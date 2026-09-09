"""Status history is append-only, and the current status is derived rather than stored."""

import re
import time
from concurrent.futures import ThreadPoolExecutor
from importlib import resources
from threading import Barrier

import pytest

from data import store
from data.repository import Repository
from recruiting.status import STATUSES

JOB = ("job-key", "https://jobs.example.com/roles/1", "Engineer", "Example Company", "remote")


def repository(path):
    instance = Repository(path)
    with store.connection(path) as conn, store.transaction(conn):
        conn.execute(
            "INSERT INTO opportunities(id,url,title,company,location) VALUES (?,?,?,?,?)", JOB
        )
    return instance


def test_history_cannot_be_edited_or_erased(tmp_path):
    path = tmp_path / "db"
    store.migrate(path)
    with store.connection(path) as conn, store.transaction(conn):
        conn.execute(
            "INSERT INTO opportunities(id,url,title,company,location) VALUES (?,?,?,?,?)", JOB
        )
        conn.execute(
            "INSERT INTO opportunity_status_history(opportunity_id,status,actor) "
            "VALUES ('job-key','new','operator')"
        )
    with store.connection(path) as conn:
        for sql in (
            "UPDATE opportunity_status_history SET status='closed'",
            "DELETE FROM opportunity_status_history",
        ):
            with pytest.raises(Exception, match="append-only"):
                conn.execute(sql)


def test_the_current_status_is_the_latest_event_not_the_latest_timestamp(tmp_path):
    """Two events can share a second, so ordering must not depend on the clock."""
    path = tmp_path / "db"
    instance = repository(path)
    for status in ("reviewing", "interested", "applied"):
        instance.record_status("job-key", status, actor="operator")
    stamps = {row[3] for row in instance.status_history("job-key")}
    assert len(stamps) == 1, "the events did not share a timestamp; the test proves nothing"
    assert instance.status("job-key") == "applied"
    assert [row[0] for row in instance.status_history("job-key")] == [
        "reviewing",
        "interested",
        "applied",
    ]


def test_a_correction_is_a_new_event_and_the_earlier_one_survives(tmp_path):
    path = tmp_path / "db"
    instance = repository(path)
    instance.record_status("job-key", "rejected", actor="operator", reason="Wrong team")
    instance.record_status("job-key", "interested", actor="operator", reason="Reopened; new role")
    assert instance.status("job-key") == "interested"
    assert [(row[0], row[2]) for row in instance.status_history("job-key")] == [
        ("rejected", "Wrong team"),
        ("interested", "Reopened; new role"),
    ]


def test_the_whole_history_reconstructs_every_state_it_passed_through(tmp_path):
    path = tmp_path / "db"
    instance = repository(path)
    walked = ["reviewing", "interested", "applied", "interviewing", "rejected", "interested"]
    for status in walked:
        instance.record_status("job-key", status, actor="operator")
    history = [row[0] for row in instance.status_history("job-key")]
    assert history == walked
    # Every intermediate state is recoverable by replaying the history, which is the whole
    # reason the current status is not stored.
    assert [history[: n + 1][-1] for n in range(len(history))] == walked


@pytest.mark.parametrize("status", STATUSES)
def test_the_database_accepts_every_status_the_domain_accepts(tmp_path, status):
    instance = repository(tmp_path / f"db-{status}")
    assert instance.record_status("job-key", status, actor="operator") == status


def test_the_stored_check_and_the_domain_vocabulary_cannot_drift():
    """One vocabulary, two places that enforce it; only this test keeps them equal."""
    sql = (resources.files("data.migrations") / "0004_status_history.sql").read_text(
        encoding="utf-8"
    )
    listed = re.search(r"status TEXT NOT NULL CHECK\(status IN \(([^)]*)\)\)", sql)
    assert listed, "the status CHECK constraint was not found in migration 0004"
    assert tuple(v.strip().strip("'") for v in listed[1].split(",")) == STATUSES


def test_the_database_refuses_a_status_the_domain_would_refuse(tmp_path):
    """Written directly, past the domain guard: the database must refuse it on its own."""
    path = tmp_path / "db"
    repository(path)
    with store.connection(path) as conn:
        for status in ("reopened", "pending", "", "NEW"):
            with pytest.raises(Exception):
                conn.execute(
                    "INSERT INTO opportunity_status_history(opportunity_id,status,actor) "
                    "VALUES ('job-key',?,'operator')",
                    (status,),
                )


def test_the_clock_is_not_the_ordering_authority(tmp_path):
    """Timestamps tie, and a backfilled or corrected row can carry an earlier one. Ordering
    by created_at reorders the history and can name the wrong current status."""
    path = tmp_path / "db"
    instance = repository(path)
    written = (("reviewing", 5000), ("interested", 1000), ("applied", 3000))
    with store.connection(path) as conn, store.transaction(conn):
        for status, stamp in written:
            conn.execute(
                "INSERT INTO opportunity_status_history"
                "(opportunity_id,status,actor,created_at) VALUES ('job-key',?,'operator',?)",
                (status, stamp),
            )
    assert [row[0] for row in instance.status_history("job-key")] == [s for s, _ in written]
    assert instance.status("job-key") == "applied"


def test_an_unknown_status_is_refused_by_the_domain_before_the_database_sees_it(tmp_path):
    """The database also refuses it, but with its own wording; the operator gets ours."""
    instance = repository(tmp_path / "db")
    for status in ("reopened", "pending", "hired"):
        with pytest.raises(ValueError, match="Unknown status"):
            instance.record_status("job-key", status, actor="operator")


def test_a_recorded_status_is_normalized_rather_than_refused(tmp_path):
    """Without normalization the database would reject these outright."""
    path = tmp_path / "db"
    instance = repository(path)
    for spelling in ("Applied", "  APPLIED  ", "Applied\n"):
        assert instance.record_status("job-key", spelling, actor="operator") == "applied"
    assert {row[0] for row in instance.status_history("job-key")} == {"applied"}


def test_an_empty_actor_is_refused_by_the_database_as_well_as_the_domain(tmp_path):
    path = tmp_path / "db"
    instance = repository(path)
    with pytest.raises(ValueError, match="An actor is required"):
        instance.record_status("job-key", "applied", actor="   ")
    for actor in (None, 17, b"operator", []):
        with pytest.raises(ValueError, match="An actor is required"):
            instance.record_status("job-key", "applied", actor=actor)
    with store.connection(path) as conn, pytest.raises(Exception):
        conn.execute(
            "INSERT INTO opportunity_status_history(opportunity_id,status,actor) "
            "VALUES ('job-key','applied','')"
        )


def test_a_reason_keeps_the_operator_s_own_line_breaks(tmp_path):
    instance = repository(tmp_path / "db")
    instance.record_status(
        "job-key", "applied", actor="operator", reason="  Sent CV\nFollow up Friday\n"
    )
    assert instance.status_history("job-key")[-1][2] == "Sent CV\nFollow up Friday"


@pytest.mark.parametrize("reason", [None, 17, b"note", ["note"]])
def test_a_reason_that_is_not_text_is_refused(tmp_path, reason):
    instance = repository(tmp_path / f"db-{type(reason).__name__}")
    with pytest.raises(ValueError, match="Reason must be text"):
        instance.record_status("job-key", "applied", actor="operator", reason=reason)


def test_a_status_for_an_unknown_opportunity_is_refused(tmp_path):
    instance = repository(tmp_path / "db")
    with pytest.raises(ValueError, match="Unknown opportunity"):
        instance.record_status("missing", "applied", actor="operator")


def test_status_history_survives_a_new_repository_instance(tmp_path):
    path = tmp_path / "db"
    repository(path).record_status("job-key", "applied", actor="operator", reason="Sent CV")
    reopened = Repository(path)
    assert reopened.status("job-key") == "applied"
    assert reopened.status_history("job-key")[-1][:3] == ("applied", "operator", "Sent CV")


def test_concurrent_updates_both_survive_and_neither_overwrites_the_other(tmp_path):
    """Append-only makes an overwrite impossible; this proves the writes actually collide."""
    path = tmp_path / "db"
    instance = repository(path)
    barrier = Barrier(2)

    def record(status):
        barrier.wait(timeout=5)
        for _ in range(20):
            try:
                return instance.record_status("job-key", status, actor=f"operator-{status}")
            except TimeoutError:
                time.sleep(0.01)
        raise AssertionError(f"{status} never obtained a write reservation")

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(record, ("applied", "withdrawn")))
    history = instance.status_history("job-key")
    recorded = [row[0] for row in history]
    # Either order is correct; what must not happen is one write replacing the other.
    assert sorted(recorded) == ["applied", "withdrawn"], recorded
    assert instance.status("job-key") == recorded[-1]
    assert {row[1] for row in history} == {"operator-applied", "operator-withdrawn"}
