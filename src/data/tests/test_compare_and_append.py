"""An operator status write acts against a state they read, so that state must still hold.

The opportunity moving between the read and the write is not a rare case to tolerate: it
is the case that decides whether the ledger means anything. An operator who read
`interested` and chose `applied` must not have that land on top of a `withdrawn` recorded
in the meantime and silently become the current status.

The rule is the one the draft path already applies to an approval -- act against a known
state, refuse when the state has moved -- so nothing new is invented here, only applied
where the operator writes.
"""

import json

import pytest

from communications.controlled import ControlledDrafts
from data import store
from data.repository import Repository
from recruiting.models import Profile
from recruiting.status import StatusConflict
from system.workflow import Workflow

BODY = json.dumps(
    {
        "jobs": [
            {
                "title": "Engineer",
                "company": "Example Company",
                "url": "https://jobs.example.com/1",
                "location": "remote",
                "skills": ["python", "sql"],
            },
            {
                "title": "Analyst",
                "company": "Other Company",
                "url": "https://jobs.example.com/2",
                "location": "remote",
                "skills": ["python"],
            },
        ]
    }
)


@pytest.fixture
def repository(tmp_path):
    instance = Repository(tmp_path / "db")
    Workflow(instance, ControlledDrafts(), Profile(("python", "sql"))).intake("one", BODY)
    return instance


@pytest.fixture
def opportunity(repository):
    return repository.opportunities()[0]["id"]


def history(repository, opportunity):
    return [(row[0], row[1]) for row in repository.status_history(opportunity)]


def current_event(repository, opportunity):
    return repository.opportunity(opportunity)["status_event"]


# --- the append ---------------------------------------------------------------------------


def test_an_append_against_the_event_it_was_read_from_is_recorded(repository, opportunity):
    read = current_event(repository, opportunity)
    recorded = repository.record_status(
        opportunity, "interested", actor="operator", reason="worth a look", expected_event_id=read
    )
    assert recorded["status"] == "interested"
    assert recorded["previous_event"] == read
    assert recorded["event"] > read
    # The event named in the result is the one that was actually written.
    assert current_event(repository, opportunity) == recorded["event"]
    assert repository.status(opportunity) == "interested"


def test_the_ruling_scenario_refuses_rather_than_burying_a_newer_status(repository, opportunity):
    """The case from the ruling, run exactly as written.

    An operator reads event 41 / `interested` and intends `applied`. Another process
    records `withdrawn` first. The first writer must not append on top of it.
    """
    read = current_event(repository, opportunity)
    repository.record_status(opportunity, "interested", actor="operator", expected_event_id=read)
    read = current_event(repository, opportunity)

    # Another process moves the opportunity.
    moved = repository.record_status(
        opportunity, "withdrawn", actor="other", reason="filled", expected_event_id=read
    )

    with pytest.raises(StatusConflict) as refused:
        repository.record_status(opportunity, "applied", actor="operator", expected_event_id=read)
    assert refused.value.expected == read
    assert refused.value.observed == moved["event"]
    assert refused.value.status == "withdrawn"
    # Nothing was written, so `withdrawn` is still what the opportunity says.
    assert repository.status(opportunity) == "withdrawn"
    assert history(repository, opportunity) == [
        ("new", "intake"),
        ("interested", "operator"),
        ("withdrawn", "other"),
    ]


def test_a_refusal_writes_nothing_at_all(repository, opportunity):
    """Not merely "no status event": the ledger is append-only, and this is not an event."""
    read = current_event(repository, opportunity)
    before = history(repository, opportunity)
    with pytest.raises(StatusConflict):
        repository.record_status(
            opportunity, "applied", actor="operator", expected_event_id=read + 1
        )
    assert history(repository, opportunity) == before
    assert current_event(repository, opportunity) == read


def test_the_refusal_says_what_it_found(repository, opportunity):
    """A refusal the operator cannot act on would just be an obstacle."""
    read = current_event(repository, opportunity)
    repository.record_status(opportunity, "rejected", actor="other", expected_event_id=read)
    with pytest.raises(StatusConflict) as refused:
        repository.record_status(opportunity, "applied", actor="operator", expected_event_id=read)
    message = str(refused.value)
    assert str(read) in message and "rejected" in message
    assert str(refused.value.observed) in message


# --- what the expectation may name ---------------------------------------------------------


def test_an_event_belonging_to_another_opportunity_never_matches(repository):
    """Ids are global to the ledger, so an expectation has to be about this opportunity."""
    first, second = (row["id"] for row in repository.opportunities()[:2])
    read = current_event(repository, second)
    with pytest.raises(StatusConflict) as refused:
        repository.record_status(first, "applied", actor="operator", expected_event_id=read)
    assert refused.value.observed == current_event(repository, first)
    assert refused.value.observed != read


@pytest.mark.parametrize("expectation", [0, -1, "41", 1.0, True, [41]])
def test_an_expectation_that_cannot_name_an_event_is_refused(repository, opportunity, expectation):
    """Zero is the unbound value and no row carries it; the rest are not event ids at all.

    True is in this list because it is an int in Python, and an expectation of 1 arrived
    at by accident would compare equal to the opening event of the first opportunity ever
    recorded.
    """
    before = history(repository, opportunity)
    with pytest.raises(ValueError, match="positive whole number"):
        repository.record_status(
            opportunity, "applied", actor="operator", expected_event_id=expectation
        )
    assert history(repository, opportunity) == before


def test_an_append_with_no_expectation_is_still_possible(repository, opportunity):
    """Intake records the opening event inside the transaction that creates the
    opportunity, where there is no prior state to have moved. Every operator-facing write
    supplies an expectation; this path is for the writes that are not decisions."""
    read = current_event(repository, opportunity)
    recorded = repository.record_status(opportunity, "applied", actor="intake")
    assert recorded["previous_event"] == read
    assert repository.status(opportunity) == "applied"


def test_an_unknown_opportunity_is_refused_before_any_expectation_is_considered(repository):
    with pytest.raises(ValueError, match="Unknown opportunity"):
        repository.record_status("no-such-job", "applied", actor="operator", expected_event_id=1)


# --- where the comparison happens -----------------------------------------------------------


def probe_write_lock(path) -> bool:
    """True when another connection cannot take the write reservation right now.

    Deliberately not store.transaction(), which retries for a second: this asks whether
    the lock is held at this instant, not whether it can eventually be acquired.
    """
    with store.connection(path) as probe:
        try:
            probe.execute("BEGIN IMMEDIATE")
        except Exception as exc:
            assert any(word in str(exc).lower() for word in ("locked", "busy")), exc
            return True
        probe.execute("ROLLBACK")
        return False


def test_the_comparison_happens_inside_the_write_reservation(repository, opportunity, monkeypatch):
    """Reading the latest event before the write would only move the race earlier.

    The value has to be read where nothing can commit between reading it and the insert
    that depends on it. That is what BEGIN IMMEDIATE buys, so this asserts the read really
    happens under it rather than trusting the arrangement of the source.
    """
    path = repository.path
    assert not probe_write_lock(path), "the lock is not held before the write starts"
    observed = []
    original = Repository._latest_event

    def watched(conn, opportunity_id):
        observed.append(probe_write_lock(path))
        return original(conn, opportunity_id)

    monkeypatch.setattr(Repository, "_latest_event", staticmethod(watched))
    repository.record_status(
        opportunity,
        "applied",
        actor="operator",
        expected_event_id=current_event(repository, opportunity),
    )
    assert observed == [True], "the latest event was read outside the write reservation"
    assert not probe_write_lock(path), "the reservation outlived the write"


def test_two_writers_starting_from_the_same_event_cannot_both_win(repository, opportunity):
    """Serialized by the reservation: whoever commits second sees the other's event."""
    read = current_event(repository, opportunity)
    first = repository.record_status(opportunity, "applied", actor="first", expected_event_id=read)
    with pytest.raises(StatusConflict) as refused:
        repository.record_status(
            opportunity, "interviewing", actor="second", expected_event_id=read
        )
    assert refused.value.observed == first["event"]
    assert repository.status(opportunity) == "applied"
    assert len(history(repository, opportunity)) == 2
