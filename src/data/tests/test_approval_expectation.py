"""An approval authorizes a destination, so it must bind the one the operator saw.

The operator reads an approval packet and decides from what it says. Between that read and
the write, a later message about the same job can move where the draft would be addressed
without changing anything else: same review, same wording, same status, new recipient. The
approval then binds an address nobody ever reviewed, and every later check passes, because
each of them compares against the address as it is now.

The rule is the one the status ledger already applies to an operator write and the draft
path already applies to an approval -- act against a known state, refuse when the state has
moved -- applied where the operator approves.
"""

import pytest

from communications.message import Message
from data import store
from data.repository import Repository
from recruiting.models import BindingConflict, Profile
from system.workflow import Intake

JOB_TEXT = (
    "Title: IAM Architect\r\n"
    "Company: Example Corp\r\n"
    "Location: remote\r\n"
    "Skills: Python, SQL\r\n"
    "URL: https://jobs.example.com/roles/1\r\n"
)


def message(sender="recruiter@example.com", external_id="m1"):
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
def review(repository, intake):
    return intake.intake_message(message())[0]


def packet(repository, review_id) -> dict:
    """The four facts an approval binds, read the way an operator's view reads them."""
    seen = repository.authorization(review_id)
    return {
        "content_digest": seen["content_digest"],
        "draft_digest": seen["draft_digest"],
        "addressing_digest": seen["addressing_digest"],
        "status_event_id": seen["status_event_id"],
    }


def decision(repository, review_id):
    seen = repository.authorization(review_id)
    return {key: value for key, value in seen.items() if key.startswith("bound_")} | {
        "approved": seen["approved"]
    }


# --- the packet the operator read ---------------------------------------------------------------


def test_a_matching_packet_records_the_decision(repository, review):
    """The ordinary case: nothing moved between the read and the write."""
    repository.decide(review, approved=True, actor="operator", expected=packet(repository, review))
    assert repository.authorization(review)["approved"] is True
    assert repository.audit(review) == ["review_created", "approved"]


def test_a_later_message_that_moves_the_recipient_refuses_the_stale_approval(
    repository, intake, review
):
    """The finding itself, end to end.

    The operator reads the packet. A later alert for the same job arrives from somebody
    else, which moves where a draft would be addressed without changing the review, its
    wording or its status. Approving from the packet they read must refuse rather than
    authorize a recipient that was never on screen.
    """
    read = packet(repository, review)
    assert intake.intake_message(message(sender="bob@example.com", external_id="m2")) == [review]
    moved = packet(repository, review)
    assert moved["addressing_digest"] != read["addressing_digest"], "the recipient did not move"
    assert {key: moved[key] for key in moved if key != "addressing_digest"} == {
        key: read[key] for key in read if key != "addressing_digest"
    }, "nothing else moved, which is what makes this silent"

    with pytest.raises(BindingConflict) as refused:
        repository.decide(review, approved=True, actor="operator", expected=read)

    assert refused.value.expected == read
    assert refused.value.observed == moved
    assert "addressing_digest" in str(refused.value)
    # Nothing was authorized: not the address the operator read, and not the one that
    # replaced it. A refusal that recorded either would be the bug wearing an exception.
    assert repository.authorization(review)["approved"] is None
    assert repository.audit(review) == ["review_created"]


def test_a_stale_expectation_writes_nothing_and_leaves_an_earlier_decision_intact(
    repository, intake, review
):
    """A refused re-approval is not a partial one: the standing decision is untouched."""
    repository.decide(review, approved=True, actor="operator", expected=packet(repository, review))
    stood = decision(repository, review)
    read = packet(repository, review)
    intake.intake_message(message(sender="bob@example.com", external_id="m2"))

    with pytest.raises(BindingConflict):
        repository.decide(review, approved=True, actor="second", expected=read)

    assert decision(repository, review) == stood, "the standing decision was mutated"
    assert repository.audit(review) == ["review_created", "approved"], "an audit event was appended"


@pytest.mark.parametrize(
    "field, value",
    [
        ("content_digest", "0" * 64),
        ("draft_digest", "0" * 64),
        ("status_event_id", 0),
    ],
)
def test_every_bound_field_is_compared(repository, review, field, value):
    """Not only the address.

    The recipient is the field a later message moves on its own, so it is the one with an
    end-to-end reproduction above. The other three are substituted here: a review's content
    and wording cannot drift under a live review id -- a changed job produces a new review
    and supersedes this one -- and a status that moved is covered separately below. What is
    asserted is the comparison, which must cover every fact the approval goes on to bind.
    """
    read = packet(repository, review) | {field: value}
    with pytest.raises(BindingConflict) as refused:
        repository.decide(review, approved=True, actor="operator", expected=read)
    assert field in str(refused.value)
    assert repository.authorization(review)["approved"] is None
    assert repository.audit(review) == ["review_created"]


def test_a_status_recorded_in_between_refuses_the_stale_packet(repository, review):
    """A real drift for the fourth field: the opportunity moved after the packet was read."""
    read = packet(repository, review)
    opportunity = repository.opportunities()[0]["id"]
    repository.record_status(opportunity, "interested", actor="operator")
    with pytest.raises(BindingConflict) as refused:
        repository.decide(review, approved=True, actor="operator", expected=read)
    assert refused.value.observed["status_event_id"] != read["status_event_id"]
    assert repository.authorization(review)["approved"] is None


# --- what the expectation does not change -------------------------------------------------------


def test_omitting_the_expectation_is_the_write_it_has_always_been(repository, intake, review):
    """The trusted-caller path is unchanged, including under the drift that would refuse."""
    intake.intake_message(message(sender="bob@example.com", external_id="m2"))
    repository.decide(review, approved=True, actor="operator")
    assert repository.authorization(review)["approved"] is True
    assert repository.audit(review) == ["review_created", "approved"]


def test_a_rejection_needs_no_packet(repository, intake, review):
    """A rejection binds nothing, so it is not put behind a precondition the risk needs.

    Recorded here under exactly the drift that refuses an approval: withdrawing interest
    must not become the one action an operator cannot take from a stale screen.
    """
    intake.intake_message(message(sender="bob@example.com", external_id="m2"))
    repository.decide(review, approved=False, actor="operator")
    assert repository.authorization(review)["approved"] is False
    assert repository.audit(review) == ["review_created", "rejected"]


# --- where the comparison happens ---------------------------------------------------------------


def probe_write_lock(path) -> bool:
    """True when another connection cannot take the write reservation right now.

    Deliberately not store.transaction(), which retries for a second: this asks whether the
    lock is held at this instant, not whether it can eventually be acquired.
    """
    with store.connection(path) as probe:
        try:
            probe.execute("BEGIN IMMEDIATE")
        except Exception as exc:
            assert any(word in str(exc).lower() for word in ("locked", "busy")), exc
            return True
        probe.execute("ROLLBACK")
        return False


def test_the_comparison_happens_inside_the_write_reservation(repository, review, monkeypatch):
    """Comparing before the write would only move the race earlier, not close it.

    The packet has to be compared where nothing can commit between the comparison and the
    decision row that depends on it. That is what BEGIN IMMEDIATE buys, so this asserts the
    comparison really reads under it rather than trusting the order of the source.
    """
    path = repository.path
    assert not probe_write_lock(path), "the lock is not held before the write starts"
    observed = []
    original = Repository._expectation

    def watched(binding):
        observed.append(probe_write_lock(path))
        return original(binding)

    monkeypatch.setattr(Repository, "_expectation", staticmethod(watched))
    repository.decide(review, approved=True, actor="operator", expected=packet(repository, review))
    assert observed == [True], "the packet was compared outside the write reservation"
    assert not probe_write_lock(path), "the reservation outlived the write"
