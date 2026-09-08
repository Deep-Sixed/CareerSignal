"""The distinct cases are written first: a false equivalence hides a job nobody reviewed."""

import pytest

from recruiting.location import UNKNOWN, Location

DISTINCT = [
    ("remote US", "remote Canada"),
    ("remote US", "remote Europe"),
    ("remote Canada", "remote Europe"),
    ("remote", "hybrid"),
    ("remote", "onsite"),
    ("hybrid", "onsite"),
    ("Philadelphia", "New York"),
    ("onsite Philadelphia", "onsite New York"),
    ("", "remote"),
    ("Philadelphia", "remote"),
    ("hybrid Philadelphia", "hybrid New York"),
    ("remote US", "onsite US"),
]


@pytest.mark.parametrize(("left", "right"), DISTINCT)
def test_distinct_locations_never_collapse(left, right):
    assert Location.parse(left) != Location.parse(right)


EQUIVALENT = [
    # The filler spellings below were found by probing beyond the first draft of this list;
    # each was a false distinction that the implementation originally produced.
    [
        "remote",
        "Remote",
        "REMOTE",
        "  remote  ",
        "fully remote",
        "100% remote",
        "remote only",
        "remote work",
        "remote job",
        "Remote Working",
        "Remote Position",
        "remote opportunity",
        "Remote.",
        "work from home",
        "WFH",
        "remote anywhere",
    ],
    [
        "remote - US",
        "remote (US)",
        "US remote",
        "Remote — U.S.",
        "remote, USA",
        "US-based remote",
        "Remote (US only)",
        "remote in US",
        "Remote work - USA",
    ],
    ["remote Canada", "Remote - Canada", "remote (canada)"],
    ["remote Europe", "remote - EU", "Remote (Europe)"],
    ["hybrid", "Hybrid", "HYBRID"],
    ["onsite", "on-site", "on site", "Onsite", "in office", "in-office"],
    ["", "   "],
]


@pytest.mark.parametrize("spellings", EQUIVALENT)
def test_equivalent_spellings_share_one_meaning(spellings):
    parsed = {Location.parse(s) for s in spellings}
    assert len(parsed) == 1, parsed


def test_parsed_structure_is_what_eligibility_compares():
    assert Location.parse("Remote (US)") == Location("remote", "us")
    assert Location.parse("fully remote") == Location("remote", "")
    assert Location.parse("hybrid") == Location("hybrid", "")
    assert Location.parse("onsite Philadelphia") == Location("onsite", "philadelphia")
    assert Location.parse("Philadelphia") == Location(UNKNOWN, "philadelphia")
    assert Location.parse("") == Location(UNKNOWN, "")


def test_unstated_work_mode_is_never_eligible():
    """Missing information stays visible instead of being read as a match."""
    remote = Location.parse("remote")
    assert not remote.accepts(Location.parse(""))
    assert not remote.accepts(Location.parse("Philadelphia"))
    assert not Location.parse("onsite Philadelphia").accepts(Location.parse("Philadelphia"))


def test_configured_region_narrows_and_absent_region_does_not():
    anywhere, in_us = Location.parse("remote"), Location.parse("remote US")
    assert anywhere.accepts(Location.parse("remote Canada"))
    assert anywhere.accepts(Location.parse("remote US"))
    assert not in_us.accepts(Location.parse("remote Canada"))
    assert in_us.accepts(Location.parse("remote US"))
    # A posting that states remote without a region is accepted under a region-constrained
    # profile, and the review reasons say the region was not stated.
    assert in_us.accepts(Location.parse("remote"))


def test_work_mode_must_match_exactly():
    for configured in ("remote", "hybrid", "onsite"):
        accepted = Location.parse(configured)
        for candidate in ("remote", "hybrid", "onsite"):
            assert accepted.accepts(Location.parse(candidate)) is (configured == candidate)


def test_description_names_the_structure_it_matched_on():
    assert Location.parse("Remote (US)").describe() == "remote / us"
    assert Location.parse("fully remote").describe() == "remote, region not stated"
    assert Location.parse("Philadelphia").describe() == "work mode not stated / philadelphia"
    assert Location.parse("").describe() == "not stated"


def test_filler_removal_does_not_merge_two_places():
    """The qualifier list is generic English; it must never collapse distinct regions."""
    for left, right in (
        ("remote in US", "remote in Canada"),
        ("onsite in London", "onsite in Berlin"),
        ("hybrid based in New York", "hybrid based in Boston"),
        ("remote work US", "remote work Europe"),
    ):
        assert Location.parse(left) != Location.parse(right)


def test_a_trailing_period_is_trimmed_without_breaking_initialisms():
    assert Location.parse("Remote.") == Location.parse("remote")
    assert Location.parse("Remote - U.S.") == Location.parse("remote US")
    assert Location.parse("onsite Philadelphia.") == Location.parse("onsite Philadelphia")
