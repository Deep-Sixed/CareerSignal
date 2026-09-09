import json

import pytest

from communications.controlled import ControlledDrafts
from communications.message import Message
from data import store
from data.repository import Repository
from recruiting.models import Opportunity, Profile
from system.workflow import Workflow


def workflow(path):
    return Workflow(Repository(path), ControlledDrafts(), Profile(("python", "sql")))


def test_text_html_jobs_provenance_replay_and_no_drafts(tmp_path):
    flow = workflow(tmp_path / "db")
    message = Message(
        "synthetic-mailbox",
        "alert-1",
        sender="alerts@example.com",
        html="<article><h2>Engineer</h2><p>Company: Example Company</p><p>Location: Remote</p>"
        '<p>Skills: Python, SQL</p><a href="https://jobs.example.com/1?utm_source=mail">Apply</a></article>'
        "<article><h2>Analyst</h2><p>Company: Example Company</p>"
        '<a href="https://jobs.example.com/2">Apply</a></article>'
        "<article><h2>Incomplete</h2></article>",
    )
    reviews = flow.intake_message(message)
    assert len(reviews) == 2
    evidence = flow.repository.extraction_evidence(message.key)
    assert len(evidence) == 3 and evidence[2][4] is None
    assert sorted(r[4] for r in evidence if r[4]) == reviews
    assert "Missing required" in evidence[2][2]
    assert flow.intake_message(message) == reviews
    assert flow.repository.extraction_evidence(message.key) == evidence
    assert workflow(tmp_path / "db").intake_message(message) == reviews
    for review in reviews:
        with pytest.raises(ValueError, match="approval"):
            flow.draft(review)
    assert flow.provider.calls == 0
    with store.connection(flow.repository.path) as conn:
        assert conn.execute("SELECT format,parser_version FROM message_sources").fetchall() == [
            ("html", "labeled-v1")
        ]
        assert conn.execute("SELECT count(*) FROM draft_intents").fetchone()[0] == 0


def test_rejected_input_is_recorded_and_changed_message_id_fails_atomically(tmp_path):
    flow = workflow(tmp_path / "db")
    message = Message("synthetic-mailbox", "one", text="Unstructured content without job fields")
    assert flow.intake_message(message) == []
    assert flow.repository.extraction_evidence(message.key)[0][2]
    with pytest.raises(ValueError, match="different content"):
        flow.intake_message(Message("synthetic-mailbox", "one", text="Changed content"))
    assert len(flow.repository.extraction_evidence(message.key)) == 1


def test_existing_baseline_database_upgrades_without_losing_reviews(tmp_path, monkeypatch):
    path = tmp_path / "db"
    files = store.migration_files()
    with monkeypatch.context() as patch:
        patch.setattr(store, "migration_files", lambda: files[:1])
        store.migrate(path)
        with store.connection(path) as conn:
            conn.execute("INSERT INTO messages VALUES ('existing','digest')")
    assert store.migrate(path) == [p.name for p in files[1:]]
    store.verify_contract(path)
    with store.connection(path) as conn:
        assert conn.execute("SELECT id FROM messages").fetchall() == [("existing",)]


def test_malformed_hidden_container_fails_before_any_intake(tmp_path):
    """A message that cannot be interpreted must not leave partial evidence behind."""
    flow = workflow(tmp_path / "db")
    message = Message("synthetic-mailbox", "unterminated", html="<div hidden>lost<h2>Engineer</h2>")
    with pytest.raises(ValueError, match="Malformed HTML"):
        flow.intake_message(message)
    with store.connection(flow.repository.path) as conn:
        for table in ("messages", "message_sources", "extraction_items", "reviews"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert flow.provider.calls == 0


def legacy_database(path, monkeypatch):
    """A pre-scoring-v2 database holding a review that advanced under the old formula."""
    files = store.migration_files()
    with monkeypatch.context() as patch:
        patch.setattr(store, "migration_files", lambda: files[:2])
        store.migrate(path)
        with store.connection(path) as conn, store.transaction(conn):
            conn.execute("INSERT INTO messages VALUES ('legacy-message','legacy-digest')")
            conn.execute(
                "INSERT INTO opportunities(id,url,title,company,location) VALUES (?,?,?,?,?)",
                (
                    Opportunity.normalize(LEGACY_JOB).key,
                    Opportunity.normalize(LEGACY_JOB).url,
                    "Engineer",
                    "Example Company",
                    "remote",
                ),
            )
            # Two configured profile skills matched against a job stating ten: the old formula
            # scored this 100 and advanced it; stated skill coverage makes it 20.
            conn.execute(
                "INSERT INTO reviews VALUES ('legacy-review',?,'digest',100,1,?,'draft')",
                (
                    Opportunity.normalize(LEGACY_JOB).key,
                    json.dumps({"score": 100, "advances": True}),
                ),
            )
            conn.execute(
                "UPDATE opportunities SET current_review='legacy-review' WHERE id=?",
                (Opportunity.normalize(LEGACY_JOB).key,),
            )
    return store.migrate(path)


LEGACY_JOB = dict(
    title="Engineer",
    company="Example Company",
    url="https://jobs.example.com/legacy",
    location="remote",
    skills=["python", "sql"] + [f"req{i}" for i in range(8)],
)


def test_a_review_scored_before_coverage_cannot_be_approved_or_drafted(tmp_path, monkeypatch):
    path = tmp_path / "db"
    assert legacy_database(path, monkeypatch) == [
        "0003_stated_skill_coverage.sql",
        "0004_status_history.sql",
        "0005_draft_authorization.sql",
        "0006_addressing_authorization.sql",
    ]
    # The backfill in 0004 reaches an opportunity that predates status history, and says in
    # the row that it was backfilled rather than claiming an operator recorded it.
    repository = Repository(path)
    opportunity = Opportunity.normalize(LEGACY_JOB).key
    assert repository.status(opportunity) == "new"
    assert repository.status_history(opportunity)[0][:3] == (
        "new",
        "migration",
        "Backfilled when status history was introduced",
    )
    repository = Repository(path)
    for act in (
        lambda: repository.decide("legacy-review", approved=True, actor="operator"),
        lambda: repository.decide("legacy-review", approved=False, actor="operator"),
        lambda: repository.claim("legacy-review"),
    ):
        with pytest.raises(ValueError, match="predates stated skill coverage"):
            act()
    # The recorded reasoning is left exactly as it was; nothing pretends it was scored under v2.
    with store.connection(path) as conn:
        row = conn.execute(
            "SELECT advances,stated_skills,coverage,payload FROM reviews WHERE id='legacy-review'"
        ).fetchone()
    assert row[0] == 1 and row[1] is None and row[2] is None
    assert json.loads(row[3])["score"] == 100


def test_a_legacy_review_is_refreshed_by_a_new_intake_event(tmp_path, monkeypatch):
    """The documented recovery: re-ingest under a new message ID, which re-scores the job."""
    path = tmp_path / "db"
    legacy_database(path, monkeypatch)
    flow = workflow(path)
    reviews = flow.intake("rescored", json.dumps({"jobs": [LEGACY_JOB]}))
    assert reviews and reviews != ["legacy-review"]
    # The new review is scored under coverage and supersedes the legacy one.
    assert flow.repository.review(reviews[0])["score"] == 20
    with pytest.raises(ValueError, match="missing or stale"):
        flow.repository.decide("legacy-review", approved=True, actor="operator")


def test_a_v2_review_of_a_job_stating_no_skills_stays_distinguishable(tmp_path):
    """Unscored under v2 is stated_skills=0; only a pre-v2 row leaves it NULL."""
    flow = workflow(tmp_path / "db")
    unscored = flow.intake("no-skills", json.dumps({"jobs": [dict(LEGACY_JOB, skills=[])]}))[0]
    with store.connection(flow.repository.path) as conn:
        row = conn.execute(
            "SELECT stated_skills,coverage FROM reviews WHERE id=?", (unscored,)
        ).fetchone()
    assert row == (0, None)
    # It is not actionable either, but for the honest reason rather than the legacy one.
    with pytest.raises(ValueError, match="below-threshold"):
        flow.repository.decide(unscored, approved=True, actor="operator")
