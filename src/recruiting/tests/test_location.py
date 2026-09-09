"""The distinct cases are written first: a false equivalence hides a job nobody reviewed."""

import pytest

from recruiting.location import AMBIGUOUS, UNKNOWN, Location

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
    ("remote", "not remote"),
    ("remote", "no remote"),
    ("remote", "remote or hybrid"),
    ("remote Canada", "remote, not Canada"),
    ("remote US", "remote, not US"),
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
    ["hybrid", "Hybrid", "HYBRID", "hybrid - not remote", "hybrid, no remote"],
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


NEGATED = [
    "not remote",
    "no remote",
    "non-remote",
    "never remote",
    "hybrid - not remote",
    "onsite only, no remote",
    "onsite only - no remote",
    "onsite (no remote option)",
    "excluding remote",
    "without remote",
    # Denial after the mode, or separated from it by filler. Prefix-only matching offered
    # remote for every one of these.
    "remote not available",
    "remote is not available",
    "remote is currently not available",
    "remote not an option",
    "remote not offered",
    "remote not possible",
    "remote unavailable",
    "remote unsupported",
    "no option for remote",
    "no remote option",
    # One mode stated under two spellings and denied once. The generated families missed
    # this too until they were extended: the second spelling re-offered the denied mode and
    # handed a remote profile a job that says it is not remote.
    "no remote/WFH option",
    "no option for remote/work from home",
    "remote/WFH not available",
    "no remote (work from home) option",
]


@pytest.mark.parametrize("posting", NEGATED)
def test_a_negated_work_mode_is_never_offered(posting):
    """Positive token matching read 'not remote' as remote, an eligible false positive."""
    assert not Location.parse("remote").accepts(Location.parse(posting))
    assert Location.parse(posting).work_mode != "remote"


def test_negation_leaves_any_other_stated_mode_intact():
    assert Location.parse("hybrid - not remote") == Location("hybrid", "")
    assert Location.parse("onsite only, no remote") == Location("onsite", "")
    assert Location.parse("onsite (no remote option)") == Location("onsite", "")
    # Negating the only stated mode leaves nothing stated rather than its opposite.
    assert Location.parse("not remote") == Location(UNKNOWN, "")


@pytest.mark.parametrize(
    "posting", ["remote or hybrid", "hybrid or onsite", "remote / onsite", "remote and hybrid"]
)
def test_several_offered_modes_fail_closed_rather_than_picking_one(posting):
    """Search order must not silently decide which of two stated meanings wins."""
    parsed = Location.parse(posting)
    assert parsed.work_mode == AMBIGUOUS and not parsed.usable
    for configured in ("remote", "hybrid", "onsite"):
        assert not Location.parse(configured).accepts(parsed)


def test_ambiguity_is_described_distinctly_from_an_absent_location():
    assert Location.parse("remote or hybrid").describe() == "more than one work mode stated"
    assert Location.parse("").describe() == "not stated"
    assert Location.parse("remote or hybrid").stated


NOT_A_MODE_DENIAL = [
    # The negation denies a region, not the work mode; the mode stays offered.
    ("remote, not Canada", "remote"),
    ("remote but not Canada", "remote"),
    ("remote US, no travel", "remote"),
    # The denial belongs to the other stated mode and must not reach across it.
    ("onsite only, no remote", "onsite"),
    ("onsite (no remote option)", "onsite"),
    ("hybrid, remote not available", "hybrid"),
    # Found by the generated invariants, not by this list. The availability word sits before
    # the mode being denied, so a forward scan from onsite reached "option" and negated the
    # mode the posting actually offers. Only word order separates these from the case above.
    ("onsite, no option for hybrid", "onsite"),
    ("remote, no option for onsite", "remote"),
    ("in office (no option for WFH)", "onsite"),
]


@pytest.mark.parametrize(("posting", "configured"), NOT_A_MODE_DENIAL)
def test_a_denial_does_not_reach_past_what_it_denies(posting, configured):
    """Over-negating would trade a false positive for a false negative."""
    assert Location.parse(configured).accepts(Location.parse(posting))


@pytest.mark.parametrize(
    ("posting", "mode"),
    [
        ("remote or WFH", "remote"),
        ("remote / work from home", "remote"),
        ("onsite or in office", "onsite"),
        ("on-site (on site)", "onsite"),
    ],
)
def test_two_spellings_of_one_mode_are_that_mode_not_two_modes(posting, mode):
    """Only genuinely different modes are ambiguous; two names for one thing are not."""
    parsed = Location.parse(posting)
    assert parsed.work_mode == mode and Location.parse(mode).accepts(parsed)


def test_a_denial_never_infers_the_opposite_mode():
    for posting in ("remote not available", "remote unavailable", "no option for remote"):
        assert Location.parse(posting) == Location(UNKNOWN, "")


def test_an_excluded_region_is_never_read_as_a_positive_one():
    """Dropping the negation turned "not Canada" into "Canada", inverting its meaning."""
    excluded = Location.parse("remote, not Canada")
    assert excluded == Location("remote", "", ("canada",))
    assert excluded != Location.parse("remote Canada")
    assert Location.parse("remote but not Canada") == excluded
    # No other region is invented in its place.
    assert excluded.region == ""


@pytest.mark.parametrize("posting", ["remote, not Canada", "remote but not Canada"])
def test_a_configured_region_rejects_a_posting_that_excludes_it(posting):
    assert not Location.parse("remote Canada").accepts(Location.parse(posting))
    # An unconstrained profile is unaffected: the posting still offers remote.
    assert Location.parse("remote").accepts(Location.parse(posting))
    # A different configured region is unaffected too.
    assert Location.parse("remote US").accepts(Location.parse(posting))


def test_several_exclusions_are_kept_separately():
    parsed = Location.parse("remote, not Canada, not Europe")
    assert parsed.excluded == ("canada", "europe") and parsed.region == ""
    assert not Location.parse("remote Europe").accepts(parsed)
    assert Location.parse("remote US").accepts(parsed)


def test_exclusions_appear_in_the_description():
    assert Location.parse("remote, not Canada").describe() == (
        "remote, region not stated, excluding canada"
    )
