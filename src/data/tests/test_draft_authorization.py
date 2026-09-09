"""An approval authorizes one external write, so it must say exactly what was approved."""

import json

import pytest

from communications.controlled import ControlledDrafts
from data import store
from data.repository import Repository
from recruiting.models import Profile
from recruiting.status import TERMINAL
from system.workflow import Workflow

JOB = dict(
    title="Engineer",
    company="Example Company",
    url="https://jobs.example.com/1",
    location="remote",
    skills=["python", "sql"],
)


def body(**changes):
    return json.dumps({"jobs": [{**JOB, **changes}]})


@pytest.fixture
def flow(tmp_path):
    return Workflow(Repository(tmp_path / "db"), ControlledDrafts(), Profile(("python", "sql")))


@pytest.fixture
def approved(flow):
    review = flow.intake("one", body())[0]
    flow.repository.decide(review, approved=True, actor="operator")
    return review


def opportunity_of(flow, review):
    with store.connection(flow.repository.path) as conn:
        return conn.execute("SELECT opportunity_id FROM reviews WHERE id=?", (review,)).fetchone()[
            0
        ]


# --- what an approval is bound to -------------------------------------------------------


def test_an_approval_records_the_wording_and_the_state_it_was_read_in(flow, approved):
    bound = flow.repository.authorization(approved)
    assert bound["approved"] is True
    assert bound["bound_content"] == bound["content_digest"]
    assert bound["bound_draft"] == bound["draft_digest"]
    assert bound["bound_event"] == bound["status_event_id"] > 0
    assert bound["status"] == "new"


def test_an_approval_bound_to_nothing_cannot_authorize_an_outward_draft(flow, approved):
    """Rows written before this contract carry no binding, and are not guessed at."""
    with store.connection(flow.repository.path) as conn, store.transaction(conn):
        conn.execute(
            "UPDATE decisions SET content_digest='',draft_digest='',status_event_id=0 "
            "WHERE review_id=?",
            (approved,),
        )
    with pytest.raises(ValueError, match="predates draft authorization binding"):
        flow.repository.claim(approved)
    assert flow.repository.intent(approved) is None


# --- what invalidates it ----------------------------------------------------------------


def test_a_rescored_review_invalidates_the_approval(flow, approved):
    """The operator approved a specific set of words about a specific opportunity."""
    with store.connection(flow.repository.path) as conn, store.transaction(conn):
        conn.execute("UPDATE reviews SET content_digest='different' WHERE id=?", (approved,))
    with pytest.raises(ValueError, match="Review changed since it was approved"):
        flow.repository.claim(approved)
    assert flow.repository.intent(approved) is None


def test_changed_draft_wording_invalidates_the_approval(flow, approved):
    with store.connection(flow.repository.path) as conn, store.transaction(conn):
        conn.execute("UPDATE reviews SET draft='Something else entirely' WHERE id=?", (approved,))
    with pytest.raises(ValueError, match="wording changed"):
        flow.repository.claim(approved)
    assert flow.repository.intent(approved) is None


@pytest.mark.parametrize("status", TERMINAL)
def test_an_ended_opportunity_does_not_authorize_an_outward_draft(flow, approved, status):
    flow.repository.record_status(
        opportunity_of(flow, approved), status, actor="operator", reason="done"
    )
    with pytest.raises(ValueError, match=f"status is {status}"):
        flow.repository.claim(approved)
    assert flow.repository.intent(approved) is None


@pytest.mark.parametrize("status", ["reviewing", "interested", "applied", "interviewing", "offer"])
def test_an_ordinary_move_through_the_pipeline_does_not_invalidate_the_approval(
    flow, approved, status
):
    """interested to applied does not mean the approved words became wrong."""
    flow.repository.record_status(
        opportunity_of(flow, approved), status, actor="operator", reason=""
    )
    claimed, state, value = flow.repository.claim(approved)
    assert claimed and state == "attempting" and value


def test_a_terminated_opportunity_can_be_revived_and_then_drafted(flow, approved):
    """Refusing is not discarding. The approval is still there when the state comes back."""
    opportunity = opportunity_of(flow, approved)
    flow.repository.record_status(opportunity, "rejected", actor="operator", reason="passed")
    with pytest.raises(ValueError):
        flow.repository.claim(approved)
    flow.repository.record_status(opportunity, "interested", actor="operator", reason="reopened")
    assert flow.repository.claim(approved)[0] is True


def test_approving_an_already_ended_opportunity_is_refused_at_the_decision(flow):
    """Refuse where the operator is looking, rather than storing an unusable approval."""
    review = flow.intake("one", body())[0]
    flow.repository.record_status(
        opportunity_of(flow, review), "withdrawn", actor="operator", reason="not pursuing"
    )
    with pytest.raises(ValueError, match="record an active status"):
        flow.repository.decide(review, approved=True, actor="operator")
    # Rejecting one is always allowed: it records agreement with where it already is.
    flow.repository.decide(review, approved=False, actor="operator")


def test_the_most_recent_source_addresses_the_draft(flow):
    """One opportunity can arrive in several messages, and the newest one is the address.

    Ordering by message_id would pick by hash: the recipient of an outward draft would be
    chosen arbitrarily, and re-ingesting a corrected message might or might not take
    effect. Row order is the arrival sequence, the same reason status history orders by id.
    """
    from communications.message import Message

    def deliver(external_id, sender):
        return flow.intake_message(
            Message(
                namespace="gmail:operator@example.com",
                external_id=external_id,
                sender=sender,
                subject="A role",
                text=(
                    "Title: Engineer\r\nCompany: Example Company\r\nLocation: remote\r\n"
                    "Skills: python, sql\r\nURL: https://jobs.example.com/1\r\n"
                ),
            )
        )[0]

    first = deliver("m1", "first@example.com")
    assert flow.repository.addressing(first)["to"] == "first@example.com"
    again = deliver("m2", "second@example.com")
    assert again == first, "the job did not change, so the review is reused"
    assert flow.repository.addressing(again)["to"] == "second@example.com"
    # And a third, to show it is ordering rather than a two-row coincidence.
    deliver("m3", "third@example.com")
    assert flow.repository.addressing(first)["to"] == "third@example.com"


# --- what the intent records ------------------------------------------------------------


def test_the_intent_records_the_status_event_the_check_actually_observed(flow, approved):
    opportunity = opportunity_of(flow, approved)
    flow.repository.record_status(opportunity, "interested", actor="operator", reason="")
    expected = flow.repository.authorization(approved)["status_event_id"]
    flow.repository.claim(approved)
    with store.connection(flow.repository.path) as conn:
        observed = conn.execute(
            "SELECT status_event_id FROM draft_intents WHERE review_id=?", (approved,)
        ).fetchone()[0]
    assert observed == expected
    # A later event does not rewrite the record of what authorized the write.
    flow.repository.record_status(opportunity, "applied", actor="operator", reason="")
    with store.connection(flow.repository.path) as conn:
        assert (
            conn.execute(
                "SELECT status_event_id FROM draft_intents WHERE review_id=?", (approved,)
            ).fetchone()[0]
            == observed
        )
    assert flow.repository.authorization(approved)["status_event_id"] > observed


def test_the_authorization_check_and_the_claim_are_one_transaction(flow, approved):
    """The check cannot pass against a state that moved before the intent was written.

    Both statements run inside claim's own BEGIN IMMEDIATE, so nothing can be appended
    between the terminal-status check and the row that reserves the write.
    """
    opportunity = opportunity_of(flow, approved)
    flow.repository.record_status(opportunity, "rejected", actor="operator", reason="")
    with pytest.raises(ValueError):
        flow.repository.claim(approved)
    # Nothing was reserved, so a later revival is still a clean first attempt rather than
    # a replay of a half-made one.
    flow.repository.record_status(opportunity, "interested", actor="operator", reason="")
    claimed, _, _ = flow.repository.claim(approved)
    assert claimed is True
    assert flow.repository.audit(approved).count("draft_intent") == 1


def test_a_refused_draft_leaves_the_approval_usable(flow, approved):
    """Refuse, correct, reevaluate: a refusal must not consume the operator's decision."""
    opportunity = opportunity_of(flow, approved)
    flow.repository.record_status(opportunity, "closed", actor="operator", reason="")
    for _ in range(3):
        with pytest.raises(ValueError):
            flow.repository.claim(approved)
    assert flow.repository.authorization(approved)["approved"] is True
    flow.repository.record_status(opportunity, "interested", actor="operator", reason="")
    assert flow.repository.claim(approved)[0] is True
