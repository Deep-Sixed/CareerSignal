"""One classification of what an opportunity needs next, shared by every surface.

`queue()` derives nothing of its own. It reads the two dictionaries the repository already
returns and names the queue they describe, so a list and a detail pane cannot disagree
about the same opportunity -- which is the whole reason it is a pure function rather than a
rule each view applies for itself.

The order of its tests is the rule. A durable intent records an attempt that was actually
made, so it wins absolutely over anything derived from the decision or the audit trail.
"""

import pytest

from communications.controlled import ControlledDrafts
from communications.message import Message
from data.repository import Repository
from recruiting.models import Profile
from system.views import QUEUES, queue
from system.workflow import Intake, OutwardActions


def summary(**changes) -> dict:
    """An advancing, scored review on an active opportunity, unless a case says otherwise."""
    return {"status": "new", "advances": True, "actionable": True} | changes


def action(**changes) -> dict:
    """No decision, nothing attempted, nothing in the audit trail."""
    return {"decision": None, "binds": None, "attempted": False, "draft": "none"} | changes


CASES = (
    (
        "an open intent is reconciled",
        summary(),
        action(draft="attempting", attempted=True),
        "reconcile",
    ),
    (
        "an uncertain intent is reconciled",
        summary(),
        action(draft="uncertain", attempted=True),
        "reconcile",
    ),
    (
        "an intent outranks a rejected decision",
        summary(),
        action(decision="rejected", binds=True, draft="attempting", attempted=True),
        "reconcile",
    ),
    (
        "a confirmed intent reads as created",
        summary(),
        action(decision="approved", binds=True, draft="confirmed", attempted=True),
        "created",
    ),
    (
        "a confirmed intent outranks an approval that no longer binds",
        summary(),
        action(decision="approved", binds=False, draft="confirmed", attempted=True),
        "created",
    ),
    (
        "an approval that no longer binds is stale",
        summary(),
        action(decision="approved", binds=False),
        "stale",
    ),
    (
        "a refused attempt does not settle anything, so the approval still stands",
        summary(),
        action(decision="approved", binds=True, draft="refused"),
        "draft",
    ),
    (
        "an approval that binds is ready to draft",
        summary(status="interested"),
        action(decision="approved", binds=True),
        "draft",
    ),
    (
        "an ended opportunity authorizes nothing, however it was approved",
        summary(status="withdrawn"),
        action(decision="approved", binds=True),
        "none",
    ),
    ("an undecided advancing review is a decision", summary(), action(), "decide"),
    (
        "an undecided review on an ended opportunity is not",
        summary(status="rejected"),
        action(),
        "none",
    ),
    (
        "a review written before coverage cannot be decided",
        summary(advances=None, actionable=False),
        action(),
        "none",
    ),
    ("a review below the threshold is not offered", summary(advances=False), action(), "none"),
    ("a rejected decision reads as rejected", summary(), action(decision="rejected"), "rejected"),
    (
        "a rejected decision outranks a refusal in the audit trail",
        summary(),
        action(decision="rejected", draft="refused"),
        "rejected",
    ),
    (
        "an attempt blocks the stale queue too, not only the draft queue",
        # The same not-yet-produced shape as below, with an approval that no longer binds:
        # the two approval queues are guarded separately and each guard is pinned.
        summary(),
        action(decision="approved", binds=False, draft="refused", attempted=True),
        "none",
    ),
    (
        "an attempt the audit trail does not explain still blocks the approval queues",
        # Not a shape the repository produces today -- `attempted` is true only while an
        # intent row exists, and every intent state is caught above. It pins the written
        # rule: once something has been attempted, neither approval queue may claim it.
        summary(),
        action(decision="approved", binds=True, draft="refused", attempted=True),
        "none",
    ),
)


@pytest.mark.parametrize(
    "row, state, expected", [case[1:] for case in CASES], ids=[case[0] for case in CASES]
)
def test_every_action_shape_lands_in_one_queue(row, state, expected):
    assert queue(row, state) == expected


def test_the_vocabulary_is_closed():
    """Every case above, and every case below, names one of the seven."""
    assert {case[3] for case in CASES} == set(QUEUES)


# --- the same classification over real repository state ------------------------------------------


JOB_TEXT = (
    "Title: IAM Architect\r\n"
    "Company: Example Corp\r\n"
    "Location: remote\r\n"
    "Skills: Python, SQL\r\n"
    "URL: https://jobs.example.com/roles/1\r\n"
)


def alert(sender="recruiter@example.com", external_id="m1"):
    return Message(
        namespace="gmail:operator@example.com",
        external_id=external_id,
        sender=sender,
        subject="A role for you",
        text=JOB_TEXT,
    )


@pytest.fixture
def repository(tmp_path):
    return Repository(tmp_path / "db")


@pytest.fixture
def intake(repository):
    return Intake(repository, Profile(("python", "sql")))


@pytest.fixture
def review(intake):
    return intake.intake_message(alert())[0]


def classify(repository):
    """Read the opportunity exactly as a view would, then ask which queue it is in."""
    record = repository.opportunity(repository.opportunities()[0]["id"])
    return queue(record, record["action"])


def test_an_undecided_advancing_review_is_offered_for_decision(repository, review):
    assert classify(repository) == "decide"


def test_an_approval_that_still_binds_is_ready_to_draft(repository, review):
    repository.decide(review, approved=True, actor="operator")
    assert classify(repository) == "draft"


def test_an_approval_the_address_moved_under_reads_as_stale(repository, intake, review):
    repository.decide(review, approved=True, actor="operator")
    assert intake.intake_message(alert(sender="bob@example.com", external_id="m2")) == [review]
    assert classify(repository) == "stale"


def test_a_created_draft_reads_as_created(repository, review):
    repository.decide(review, approved=True, actor="operator")
    assert OutwardActions(repository, ControlledDrafts()).draft(review)
    assert classify(repository) == "created"


def test_an_open_intent_reads_as_reconcile(repository, review):
    repository.decide(review, approved=True, actor="operator")
    assert repository.claim(review)["claimed"] is True
    assert classify(repository) == "reconcile"


def test_a_rejected_decision_reads_as_rejected(repository, review):
    repository.decide(review, approved=False, actor="operator")
    assert classify(repository) == "rejected"


def test_an_ended_opportunity_offers_nothing(repository, review):
    opportunity = repository.opportunities()[0]["id"]
    repository.record_status(opportunity, "withdrawn", actor="operator", reason="not for me")
    assert classify(repository) == "none"
