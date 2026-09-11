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
    claim = flow.repository.claim(approved)
    assert claim["claimed"] and claim["state"] == "attempting"
    assert claim["material"]["body"]


def test_a_terminated_opportunity_can_be_revived_and_then_drafted(flow, approved):
    """Refusing is not discarding. The approval is still there when the state comes back."""
    opportunity = opportunity_of(flow, approved)
    flow.repository.record_status(opportunity, "rejected", actor="operator", reason="passed")
    with pytest.raises(ValueError):
        flow.repository.claim(approved)
    flow.repository.record_status(opportunity, "interested", actor="operator", reason="reopened")
    assert flow.repository.claim(approved)["claimed"] is True


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


def deliver(flow, external_id, sender, subject="A role"):
    from communications.message import Message

    return flow.intake_message(
        Message(
            namespace="gmail:operator@example.com",
            external_id=external_id,
            sender=sender,
            subject=subject,
            text=(
                "Title: Engineer\r\nCompany: Example Company\r\nLocation: remote\r\n"
                "Skills: python, sql\r\nURL: https://jobs.example.com/1\r\n"
            ),
        )
    )[0]


# --- an approval authorizes a target, not only a body -----------------------------------


def test_an_approval_does_not_migrate_onto_a_new_recipient(flow):
    """The review is reused across messages, so a later sender could move the target.

    Nothing else about the approval changes: same content digest, same wording, same
    status. Without binding the address, an approval to write to one person would
    authorize writing to another.
    """
    review = deliver(flow, "m1", "jane@example.com")
    flow.repository.decide(review, approved=True, actor="operator")
    assert deliver(flow, "m2", "bob@example.com") == review

    bound = flow.repository.authorization(review)
    assert bound["content_digest"] == bound["bound_content"], "the review itself is unchanged"
    assert bound["bound_addressing"] != bound["addressing_digest"]
    with pytest.raises(ValueError, match="Addressing changed since approval"):
        flow.repository.claim(review)
    assert flow.repository.intent(review) is None, "a refused claim reserved nothing"


def test_a_changed_subject_also_needs_a_new_decision(flow):
    review = deliver(flow, "m1", "jane@example.com", subject="A role")
    flow.repository.decide(review, approved=True, actor="operator")
    deliver(flow, "m2", "jane@example.com", subject="Following up about the role")
    with pytest.raises(ValueError, match="Addressing changed since approval"):
        flow.repository.claim(review)


def test_the_same_target_arriving_again_is_not_a_new_outward_action(flow):
    """The guard must not refuse a repeat of the address the operator already approved.

    The message id is provenance and is deliberately outside the digest: the same sender
    and subject arriving in a second message is not a materially different action, and
    refusing it would make the guard fire on ordinary duplicate alerts.
    """
    review = deliver(flow, "m1", "jane@example.com")
    flow.repository.decide(review, approved=True, actor="operator")
    assert deliver(flow, "m2", "jane@example.com") == review
    claim = flow.repository.claim(review)
    assert claim["claimed"] is True
    assert claim["material"]["to"] == "jane@example.com"


def test_re_approving_binds_the_new_target(flow):
    review = deliver(flow, "m1", "jane@example.com")
    flow.repository.decide(review, approved=True, actor="operator")
    deliver(flow, "m2", "bob@example.com")
    with pytest.raises(ValueError, match="Addressing changed since approval"):
        flow.repository.claim(review)
    flow.repository.decide(review, approved=True, actor="operator")
    claim = flow.repository.claim(review)
    assert claim["material"]["to"] == "bob@example.com"


def test_an_approval_bound_to_no_addressing_cannot_authorize_an_outward_draft(flow):
    """Approvals written before this binding existed are refused, not guessed at."""
    review = deliver(flow, "m1", "jane@example.com")
    flow.repository.decide(review, approved=True, actor="operator")
    with store.connection(flow.repository.path) as conn, store.transaction(conn):
        conn.execute("UPDATE decisions SET addressing_digest='' WHERE review_id=?", (review,))
    with pytest.raises(ValueError, match="predates draft authorization binding"):
        flow.repository.claim(review)


def test_the_claim_hands_back_the_material_it_verified(flow):
    """Closes the window between checking an address and using it.

    Re-reading the address after the claim would let a source arriving in that interval
    move the target of an already authorized write. The claim returns what it checked, so
    there is nothing to re-read.
    """
    review = deliver(flow, "m1", "jane@example.com")
    flow.repository.decide(review, approved=True, actor="operator")
    claim = flow.repository.claim(review)
    # A competing source lands after the claim committed.
    deliver(flow, "m2", "bob@example.com")
    assert flow.repository.addressing(review)["to"] == "bob@example.com"
    # What the claim authorized is unchanged, and it is what the caller holds.
    assert claim["material"]["to"] == "jane@example.com"
    with store.connection(flow.repository.path) as conn:
        recorded = conn.execute(
            "SELECT source_message_id FROM draft_intents WHERE review_id=?", (review,)
        ).fetchone()[0]
    assert recorded == flow.repository.authorization(review)["bound_source"]


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


def test_an_unconfirmed_intent_can_never_carry_a_receipt(flow, approved):
    """Pins the invariant the replay branch leans on.

    Workflow.draft returns a receipt only for a confirmed intent. That test would pass
    even without the condition, because the schema already forbids a receipt on any other
    state -- so the condition looks redundant to a mutation run. It is kept because
    application correctness should not rest silently on a database CHECK, and this test
    makes the dependency explicit: relax the constraint and this fails rather than the
    workflow quietly starting to hand back receipts for unfinished attempts.
    """
    flow.repository.claim(approved)
    with store.connection(flow.repository.path) as conn:
        for state in ("attempting", "uncertain"):
            with pytest.raises(Exception):
                conn.execute(
                    "UPDATE draft_intents SET state=?,receipt='forged' WHERE review_id=?",
                    (state, approved),
                )
        assert (
            conn.execute(
                "SELECT receipt FROM draft_intents WHERE review_id=?", (approved,)
            ).fetchone()[0]
            is None
        )


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
    assert flow.repository.claim(approved)["claimed"] is True
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
    assert flow.repository.claim(approved)["claimed"] is True


# --- an approval and an intent bind a provider and a namespace, not only a target -------


def test_an_approval_binds_the_default_destination(flow, approved):
    """Absent any other instruction, an approval binds the same in-memory destination."""
    bound = flow.repository.authorization(approved)
    assert bound["bound_provider"] == "controlled"
    assert bound["bound_provider_namespace"] == "controlled"


def test_approved_identity_reads_back_what_was_approved(flow):
    review = flow.intake("one", body())[0]
    flow.repository.decide(
        review,
        approved=True,
        actor="operator",
        provider="gmail",
        provider_namespace="gmail:alice@example.com",
    )
    assert flow.repository.approved_identity(review) == {
        "provider": "gmail",
        "provider_namespace": "gmail:alice@example.com",
    }


def test_approved_identity_is_none_with_no_decision_at_all(flow):
    review = flow.intake("one", body())[0]
    assert flow.repository.approved_identity(review) is None


def test_approved_identity_is_none_for_a_rejection(flow):
    """A rejection authorizes no destination, not the default one."""
    review = flow.intake("one", body())[0]
    flow.repository.decide(review, approved=False, actor="operator")
    assert flow.repository.approved_identity(review) is None


def test_approved_identity_is_none_for_a_legacy_approval(flow, approved):
    """Rows written before this contract carry no provider binding, and none is guessed."""
    with store.connection(flow.repository.path) as conn, store.transaction(conn):
        conn.execute(
            "UPDATE decisions SET provider='',provider_namespace='' WHERE review_id=?",
            (approved,),
        )
    assert flow.repository.approved_identity(approved) is None


def test_a_different_provider_does_not_authorize_the_claim(flow, approved):
    """An approval for one destination is not discharged by a claim for another."""
    with pytest.raises(ValueError, match="Approved provider or mailbox changed"):
        flow.repository.claim(
            approved, provider="gmail", provider_namespace="gmail:alice@example.com"
        )
    assert flow.repository.intent(approved) is None


def test_a_different_namespace_under_the_same_provider_does_not_authorize_the_claim(flow):
    review = flow.intake("one", body())[0]
    flow.repository.decide(
        review,
        approved=True,
        actor="operator",
        provider="gmail",
        provider_namespace="gmail:alice@example.com",
    )
    with pytest.raises(ValueError, match="Approved provider or mailbox changed"):
        flow.repository.claim(review, provider="gmail", provider_namespace="gmail:bob@example.com")
    assert flow.repository.intent(review) is None


def test_re_approving_binds_the_new_destination(flow):
    review = flow.intake("one", body())[0]
    flow.repository.decide(review, approved=True, actor="operator")
    with pytest.raises(ValueError, match="Approved provider or mailbox changed"):
        flow.repository.claim(
            review, provider="gmail", provider_namespace="gmail:alice@example.com"
        )
    flow.repository.decide(
        review,
        approved=True,
        actor="operator",
        provider="gmail",
        provider_namespace="gmail:alice@example.com",
    )
    claim = flow.repository.claim(
        review, provider="gmail", provider_namespace="gmail:alice@example.com"
    )
    assert claim["claimed"] is True


def test_the_intent_records_the_provider_identity_the_claim_verified(flow):
    review = flow.intake("one", body())[0]
    flow.repository.decide(
        review,
        approved=True,
        actor="operator",
        provider="gmail",
        provider_namespace="gmail:alice@example.com",
    )
    flow.repository.claim(review, provider="gmail", provider_namespace="gmail:alice@example.com")
    assert flow.repository.intent_identity(review) == {
        "provider": "gmail",
        "provider_namespace": "gmail:alice@example.com",
    }


def test_an_approval_bound_to_no_provider_identity_cannot_authorize_an_outward_draft(
    flow, approved
):
    """Rows written before this contract carry no provider binding, and none is guessed."""
    with store.connection(flow.repository.path) as conn, store.transaction(conn):
        conn.execute(
            "UPDATE decisions SET provider='',provider_namespace='' WHERE review_id=?",
            (approved,),
        )
    with pytest.raises(ValueError, match="predates draft authorization binding"):
        flow.repository.claim(approved)
    assert flow.repository.intent(approved) is None


def test_a_legacy_intent_with_no_provider_identity_never_authorizes_another_write(flow, approved):
    """An old intent's destination was never recorded. Nothing here invents one for it."""
    flow.repository.claim(approved)
    with store.connection(flow.repository.path) as conn, store.transaction(conn):
        conn.execute(
            "UPDATE draft_intents SET provider='',provider_namespace='' WHERE review_id=?",
            (approved,),
        )
    assert flow.repository.intent_identity(approved) is None
