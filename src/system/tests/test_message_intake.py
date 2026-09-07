import pytest

from communications.controlled import ControlledDrafts
from communications.message import Message
from data import store
from data.repository import Repository
from recruiting.models import Profile
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
    assert store.migrate(path) == ["0002_message_extraction.sql"]
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
