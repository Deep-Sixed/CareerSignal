"""Read projections: what a view can ask for without deriving it a second time.

Every one of these is read-only and adds no rule. They exist so a surface can show what is
already stored -- which messages arrived, what was extracted from them, which message an
opportunity would currently be addressed from, and what has happened lately -- without
reimplementing an ordering or a count the write paths already decided.
"""

import pytest

from communications.message import Message
from data import store
from data.repository import Repository
from recruiting.models import Profile
from system.workflow import Intake

JOB_TEXT = (
    "Title: IAM Architect\r\n"
    "Company: Example Corp\r\n"
    "Location: remote\r\n"
    "Skills: Python, SQL\r\n"
    "URL: https://jobs.example.com/roles/1\r\n"
)


def message(sender="recruiter@example.com", external_id="m1", text=JOB_TEXT, subject="A role"):
    return Message(
        namespace="gmail:operator@example.com",
        external_id=external_id,
        sender=sender,
        subject=subject,
        text=text,
    )


@pytest.fixture
def repository(tmp_path):
    return Repository(tmp_path / "db")


@pytest.fixture
def intake(repository):
    return Intake(repository, Profile(("python", "sql")))


# --- communications() ---------------------------------------------------------------------------


def test_every_message_is_listed_in_arrival_order_with_its_counts(repository, intake):
    """Arrival order, not message-id order: an id is a digest and sorts by hash."""
    # Ingested newest-label first, so arrival order and id order genuinely disagree.
    arrived = [f"m{index}" for index in range(6, 0, -1)]
    for external_id in arrived:
        intake.intake_message(message(external_id=external_id, subject=f"A role {external_id}"))
    listed = repository.communications()

    assert [row["external_id"] for row in listed] == arrived, "not in arrival order"
    assert [row["arrival"] for row in listed] == sorted(row["arrival"] for row in listed)
    assert len({row["arrival"] for row in listed}) == len(arrived), "arrival does not separate them"
    assert sorted(listed, key=lambda row: row["message"]) != listed, (
        "these ids happen to hash into arrival order, so this fixture cannot tell the two "
        "orderings apart; vary the messages until it can"
    )
    first = listed[0]
    assert first["sender"] == "recruiter@example.com"
    assert first["subject"] == f"A role {arrived[0]}"
    assert first["namespace"] == "gmail:operator@example.com"
    assert (first["format"], first["parser_version"]) == ("text", "labeled-v1")
    assert (first["extracted"], first["unextracted"]) == (1, 0)


def test_the_counts_are_the_evidence_rows_not_a_second_opinion(repository, intake):
    """A message that yields one job and one unusable item is reported as exactly that."""
    intake.intake_message(message(text=JOB_TEXT + "\r\nTitle: Nothing Else Here\r\n"))
    row = repository.communications()[0]
    evidence = repository.extraction_evidence(row["message"])
    assert row["extracted"] == len([item for item in evidence if not item[2]])
    assert row["unextracted"] == len([item for item in evidence if item[2]])
    assert row["unextracted"] >= 1, "the fixture stopped producing an unextracted item"


def test_a_message_with_no_evidence_rows_counts_zero_not_null(repository, intake):
    intake.intake_message(message())
    with store.connection(repository.path) as conn, store.transaction(conn):
        conn.execute("DELETE FROM extraction_items")
    row = repository.communications()[0]
    assert (row["extracted"], row["unextracted"]) == (0, 0)


# --- communication() ----------------------------------------------------------------------------


def test_one_message_carries_its_evidence_and_what_it_currently_addresses(repository, intake):
    review = intake.intake_message(message())[0]
    opportunity = repository.opportunities()[0]["id"]
    record = repository.communication(repository.communications()[0]["message"])

    assert record["external_id"] == "m1"
    assert record["items"] == repository.extraction_evidence(record["message"])
    assert record["addresses"] == [opportunity]
    assert repository.addressing(review)["source"] == record["message"]


def test_a_superseded_source_keeps_its_evidence_and_stops_addressing(repository, intake):
    """`addresses` is where a draft would go now, not everywhere the message ever appeared."""
    review = intake.intake_message(message())[0]
    assert intake.intake_message(message(sender="bob@example.com", external_id="m2")) == [review]
    first, second = repository.communications()

    superseded = repository.communication(first["message"])
    current = repository.communication(second["message"])
    assert superseded["addresses"] == [], "a superseded source still claims the opportunity"
    assert current["addresses"] == [repository.opportunities()[0]["id"]]
    assert superseded["items"], "evidence was dropped along with the addressing"
    assert repository.addressing(review)["source"] == second["message"]


def test_an_unknown_message_is_a_key_error(repository):
    with pytest.raises(KeyError):
        repository.communication("no-such-message")


# --- sources() ----------------------------------------------------------------------------------


def test_sources_are_oldest_first_and_end_at_the_one_addressing_binds(repository, intake):
    review = intake.intake_message(message())[0]
    intake.intake_message(message(sender="bob@example.com", external_id="m2"))
    opportunity = repository.opportunities()[0]["id"]

    listed = repository.sources(opportunity)
    assert [row["external_id"] for row in listed] == ["m1", "m2"]
    assert [row["arrival"] for row in listed] == sorted(row["arrival"] for row in listed)
    bound = repository.addressing(review)
    assert listed[-1]["message"] == bound["source"]
    assert listed[-1]["sender"] == bound["to"]
    assert listed[-1]["subject"] == bound["subject"]
    assert {row["review"] for row in listed} == {review}


def test_an_opportunity_with_no_stored_sources_lists_nothing(repository, intake):
    """`intake()` records provenance without a message source, which addresses nothing."""
    intake.intake(
        "raw-1",
        '{"jobs": [{"title": "Engineer", "company": "Example Corp", '
        '"url": "https://jobs.example.com/roles/2", "location": "remote", '
        '"skills": ["python", "sql"]}]}',
    )
    opportunity = repository.opportunities()[0]["id"]
    assert repository.sources(opportunity) == []
    assert repository.addressing(repository.opportunity(opportunity)["review"])["source"] == ""


# --- timeline() ---------------------------------------------------------------------------------


# Beyond any clock a test run can produce, so these events sort above the real ones the
# fixture already wrote and `since` can isolate them.
LATER = 4_000_000_000


def historic(repository, opportunity, review, rows):
    """Insert events with chosen clocks. Append-only forbids update and delete, not insert."""
    with store.connection(repository.path) as conn, store.transaction(conn):
        for kind, created_at, value in rows:
            if kind == "status":
                conn.execute(
                    "INSERT INTO opportunity_status_history"
                    "(opportunity_id,status,actor,reason,created_at) VALUES (?,?,?,'',?)",
                    (opportunity, value, "operator", created_at),
                )
            else:
                conn.execute(
                    "INSERT INTO audit(review_id,event,created_at) VALUES (?,?,?)",
                    (review, value, created_at),
                )


def test_both_ledgers_appear_newest_first_and_say_which_they_came_from(repository, intake):
    review = intake.intake_message(message())[0]
    opportunity = repository.opportunities()[0]["id"]
    repository.record_status(opportunity, "interested", actor="operator", reason="worth a look")
    repository.decide(review, approved=True, actor="operator")

    events = repository.timeline()
    assert {event["kind"] for event in events} == {"status", "audit"}
    assert events == sorted(
        events, key=lambda event: (event["created_at"], event["event_id"]), reverse=True
    )
    status = next(event for event in events if event["event"] == "interested")
    assert (status["kind"], status["opportunity"], status["actor"]) == (
        "status",
        opportunity,
        "operator",
    )
    assert status["reason"] == "worth a look" and status["review"] is None
    approved = next(event for event in events if event["event"] == "approved")
    assert (approved["kind"], approved["review"], approved["opportunity"]) == (
        "audit",
        review,
        opportunity,
    )
    assert approved["actor"] is None and approved["reason"] is None


def test_since_is_an_inclusive_lower_bound_on_both_ledgers(repository, intake):
    """Second resolution is why: an exclusive bound would drop a co-timed event.

    Two events share the boundary second, one in each ledger. Both must survive `since`
    naming that second -- the caller may already have seen one of them, and a repeat is
    recoverable where an omission is not.
    """
    review = intake.intake_message(message())[0]
    opportunity = repository.opportunities()[0]["id"]
    historic(
        repository,
        opportunity,
        review,
        [
            ("status", LATER + 1000, "reviewing"),
            ("status", LATER + 2000, "interested"),
            ("audit", LATER + 2000, "approved"),
            ("status", LATER + 3000, "applied"),
        ],
    )

    at_the_boundary = repository.timeline(since=LATER + 2000)
    assert [event["created_at"] for event in at_the_boundary] == [
        LATER + 3000,
        LATER + 2000,
        LATER + 2000,
    ], "the boundary second was dropped, or an earlier event survived"
    # Across two independent id sequences a shared second has no true order, so this asserts
    # both are present rather than pretending one came first.
    assert {(event["kind"], event["event"]) for event in at_the_boundary[1:]} == {
        ("status", "interested"),
        ("audit", "approved"),
    }
    assert repository.timeline(since=LATER + 3001) == []


def test_the_limit_is_applied_after_ordering_and_must_be_positive(repository, intake):
    review = intake.intake_message(message())[0]
    opportunity = repository.opportunities()[0]["id"]
    historic(
        repository,
        opportunity,
        review,
        [("status", LATER + clock, "interested") for clock in (1000, 2000, 3000)],
    )
    assert [event["created_at"] for event in repository.timeline(limit=2, since=LATER + 1000)] == [
        LATER + 3000,
        LATER + 2000,
    ]
    for refused in (0, -1):
        # A negative LIMIT means "no limit" to SQLite, which would turn a bounded read
        # into an unbounded one rather than refusing.
        with pytest.raises(ValueError, match="positive"):
            repository.timeline(limit=refused)
