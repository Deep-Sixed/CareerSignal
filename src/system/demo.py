"""Synthetic certification flow; no private data or external service access."""

import json

from communications.controlled import ControlledDrafts
from data.repository import Repository
from data.store import verify_contract
from recruiting.models import Profile
from system.workflow import Intake, OutwardActions


def golden_workflow(path):
    repository = Repository(path)
    provider = ControlledDrafts()
    intake = Intake(repository, Profile(("python", "sql")))
    actions = OutwardActions(repository, provider)
    body = json.dumps(
        {
            "jobs": [
                {
                    "title": "Application Engineer",
                    "company": "Example Company",
                    "url": "https://jobs.example.com/roles/1",
                    "location": "remote",
                    "skills": ["Python", "SQL"],
                }
            ]
        }
    )
    review_id = intake.intake("synthetic-message-1", body)[0]
    if repository.intent(review_id) is None:
        try:
            actions.draft(review_id)
        except ValueError:
            pass
        else:
            raise AssertionError("Unapproved draft was allowed")
        repository.decide(review_id, approved=True, actor="synthetic-operator")
    receipt = actions.draft(review_id)
    assert receipt and actions.draft(review_id) == receipt
    assert intake.intake("synthetic-message-1", body) == [review_id]
    assert intake.intake("synthetic-message-2", body) == [review_id]
    assert provider.calls <= 1
    verify_contract(path)
    events = repository.audit(review_id)
    assert "approved" in events and "draft_confirmed" in events
    return {
        "mode": "synthetic-controlled",
        "review": review_id,
        "receipt": receipt,
        "score": repository.review(review_id)["score"],
        "audit": events,
        "replay": "PASS",
    }
