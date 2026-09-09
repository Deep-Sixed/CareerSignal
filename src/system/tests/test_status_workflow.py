"""Status is a record of what the operator decided, and touches nothing else."""

import json

import pytest

from communications.controlled import ControlledDrafts
from data import store
from data.repository import Repository
from recruiting.models import Profile
from recruiting.status import STATUSES
from system.workflow import Workflow

BODY = json.dumps(
    {
        "jobs": [
            {
                "title": "Application Engineer",
                "company": "Example Company",
                "url": "https://jobs.example.com/roles/1",
                "location": "remote",
                "skills": ["Python", "SQL"],
            },
            {
                "title": "Support Analyst",
                "company": "Example Company",
                "url": "https://jobs.example.com/roles/2",
                "location": "remote",
                "skills": ["SQL"],
            },
        ]
    }
)


def workflow(path):
    return Workflow(Repository(path), ControlledDrafts(), Profile(("python", "sql")))


def opportunities(path):
    with store.connection(path) as conn:
        rows = conn.execute("SELECT id FROM opportunities ORDER BY id").fetchall()
    return [row[0] for row in rows]


def snapshot(path):
    """Everything status must not touch."""
    with store.connection(path) as conn:
        return {
            table: conn.execute(f"SELECT * FROM {table}").fetchall()
            for table in (
                "messages",
                "opportunities",
                "reviews",
                "provenance",
                "decisions",
                "draft_intents",
                "extraction_items",
                "audit",
            )
        }


def test_every_opportunity_begins_at_new(tmp_path):
    path = tmp_path / "db"
    flow = workflow(path)
    flow.intake("message-1", BODY)
    assert opportunities(path)
    for opportunity in opportunities(path):
        assert flow.repository.status(opportunity) == "new"
        history = flow.repository.status_history(opportunity)
        assert len(history) == 1
        assert history[0][:3] == ("new", "intake", "")


def test_replaying_intake_does_not_duplicate_status_events(tmp_path):
    path = tmp_path / "db"
    flow = workflow(path)
    flow.intake("message-1", BODY)
    before = {o: flow.repository.status_history(o) for o in opportunities(path)}
    # The same message again, and a different message carrying the same jobs.
    flow.intake("message-1", BODY)
    flow.intake("message-2", BODY)
    flow.intake("message-3", BODY)
    after = {o: flow.repository.status_history(o) for o in opportunities(path)}
    assert after == before
    assert all(len(history) == 1 for history in after.values())


def test_a_recorded_status_is_not_erased_by_a_later_intake(tmp_path):
    """An opportunity re-offered by a new message keeps the status the operator gave it."""
    path = tmp_path / "db"
    flow = workflow(path)
    flow.intake("message-1", BODY)
    first = opportunities(path)[0]
    flow.repository.record_status(first, "applied", actor="operator", reason="Sent CV")
    flow.intake("message-2", BODY)
    assert flow.repository.status(first) == "applied"
    assert [row[0] for row in flow.repository.status_history(first)] == ["new", "applied"]


def test_changing_status_alters_nothing_else(tmp_path):
    path = tmp_path / "db"
    flow = workflow(path)
    review = flow.intake("message-1", BODY)[0]
    flow.repository.decide(review, approved=True, actor="operator")
    flow.draft(review)
    before = snapshot(path)
    for opportunity in opportunities(path):
        for status in STATUSES:
            flow.repository.record_status(opportunity, status, actor="operator", reason="Walk")
    assert snapshot(path) == before


def test_recording_a_status_never_creates_a_draft_or_an_approval(tmp_path):
    path = tmp_path / "db"
    flow = workflow(path)
    flow.intake("message-1", BODY)
    for opportunity in opportunities(path):
        for status in ("interested", "applied", "interviewing", "offer"):
            flow.repository.record_status(opportunity, status, actor="operator")
    with store.connection(path) as conn:
        assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM draft_intents").fetchone()[0] == 0
    assert flow.provider.calls == 0
    assert len(flow.repository.status_history(opportunities(path)[0])) == 5


def test_an_approved_review_still_needs_its_own_decision_regardless_of_status(tmp_path):
    """Status is not authorization: marking an opportunity applied approves nothing."""
    path = tmp_path / "db"
    flow = workflow(path)
    review = flow.intake("message-1", BODY)[0]
    flow.repository.record_status(opportunities(path)[0], "applied", actor="operator")
    with pytest.raises(ValueError, match="approval"):
        flow.draft(review)
    flow.repository.decide(review, approved=True, actor="operator")
    assert flow.draft(review)
