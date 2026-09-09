"""What may not be recorded is written first: an invented status is a silent wrong answer."""

import pytest

from recruiting.status import ACTIVE, INITIAL, STATUSES, validate

REFUSED = [
    "",
    "   ",
    "reopened",
    "re-opened",
    "in progress",
    "NEW!",
    "newer",
    "appliedd",
    "pending",
    "accepted",
    "hired",
    "ghosted",
    "screening",
    "new,reviewing",
    "new reviewing",
    "status",
    "null",
    "none",
]


@pytest.mark.parametrize("status", REFUSED)
def test_a_status_outside_the_vocabulary_is_refused(status):
    with pytest.raises(ValueError, match="Unknown status|must be text"):
        validate(status)


@pytest.mark.parametrize("status", [None, 17, 1.5, True, [], {}, ("new",), b"new"])
def test_a_status_that_is_not_text_is_refused(status):
    with pytest.raises(ValueError, match="must be text"):
        validate(status)


@pytest.mark.parametrize("status", STATUSES)
def test_every_listed_status_is_accepted(status):
    assert validate(status) == status


@pytest.mark.parametrize("status", STATUSES)
def test_spelling_is_normalized_rather_than_multiplying_the_vocabulary(status):
    for spelling in (status.upper(), status.capitalize(), f"  {status}  ", f"{status}\n"):
        assert validate(spelling) == status


def test_the_vocabulary_is_what_the_contract_named():
    assert STATUSES == (
        "new",
        "reviewing",
        "interested",
        "applied",
        "interviewing",
        "offer",
        "rejected",
        "withdrawn",
        "closed",
    )
    assert INITIAL == "new" and INITIAL in STATUSES
    assert set(ACTIVE) < set(STATUSES)
    assert len(set(STATUSES)) == len(STATUSES)


def test_no_transition_is_forbidden_because_a_real_search_jumps():
    """The vocabulary is validated; the order is not a rule until one is actually needed."""
    for source in STATUSES:
        for target in STATUSES:
            assert validate(target) == target, (source, target)
