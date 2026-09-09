"""Invariants over generated postings.

The hand-written cases in test_location.py can only contain what someone thought of. Every
defect found in location parsing so far sat outside that list, so these tests state rules
that must hold for a whole generated family instead of naming individual strings.

Generation is deterministic and uses only the standard library: the same phrases in the same
order on every run, so a failure names one exact posting and reproduces immediately.
"""

import itertools

from recruiting.location import AMBIGUOUS, UNKNOWN, Location

# Spellings whose intended meaning is fixed by construction. Each key is what the phrases
# under it are meant to say; the invariants below compare against that intent, never against
# what the parser happens to return.
MODE_SPELLINGS = {
    "remote": ("remote", "fully remote", "100% remote", "work from home", "WFH"),
    "hybrid": ("hybrid",),
    "onsite": ("onsite", "on-site", "on site", "in office"),
}
REGION_SPELLINGS = {
    "us": ("US", "USA", "U.S.", "United States"),
    "canada": ("Canada",),
    "europe": ("Europe", "EU"),
}
MODES = tuple(MODE_SPELLINGS)
REGIONS = tuple(REGION_SPELLINGS)

# Ways of denying a mode. The first group precedes the mode, the second follows it.
DENIAL_TEMPLATES = (
    "not {mode}",
    "no {mode}",
    "never {mode}",
    "excluding {mode}",
    "without {mode}",
    "no option for {mode}",
    "{mode} not available",
    "{mode} is not available",
    "{mode} not an option",
    "{mode} not offered",
    "{mode} unavailable",
    "{mode} unsupported",
)
# Ways of ruling out a region. These deny a place, never the work mode.
REGION_EXCLUSIONS = ("not {region}", "no {region}", "excluding {region}")
JOINERS = (", ", " - ", " ", ", but ", " (", "; ")


def joined(left: str, right: str, joiner: str) -> str:
    return f"{left}{joiner}{right}{')' if joiner == ' (' else ''}"


def modes_of(text: str) -> set:
    """The single mode a parse offers, as a set, so it can be compared for growth."""
    parsed = Location.parse(text)
    return {parsed.work_mode} if parsed.usable else set()


def every_profile():
    """Each configured location an operator could hold, with and without a region."""
    for mode in MODES:
        yield Location(mode, "")
        for region in REGIONS:
            yield Location(mode, region)


# --- Invariant 1: an excluded region is never accepted by a profile configured for it ----

EXCLUSION_CASES = [
    (mode, region, joined(mode_text, template.format(region=region_text), joiner))
    for mode, mode_texts in MODE_SPELLINGS.items()
    for mode_text in mode_texts
    for region, region_texts in REGION_SPELLINGS.items()
    for region_text in region_texts
    for template in REGION_EXCLUSIONS
    for joiner in JOINERS
]


def report(failures, total):
    return f"{len(failures)} of {total} generated postings, first: {failures[:4]}"


def test_an_excluded_region_is_never_accepted_by_a_profile_configured_for_it():
    failures = [
        posting
        for mode, region, posting in EXCLUSION_CASES
        if Location(mode, region).accepts(Location.parse(posting))
    ]
    assert not failures, report(failures, len(EXCLUSION_CASES))


def test_an_excluded_region_never_becomes_the_positive_region():
    """Dropping the negation once turned "not Canada" into "Canada"; it must never return."""
    failures = [
        posting
        for _, region, posting in EXCLUSION_CASES
        if Location.parse(posting).region == region
    ]
    assert not failures, report(failures, len(EXCLUSION_CASES))


# --- Invariant 2: a denied sole work mode is never eligible ------------------------------

SOLE_DENIAL_CASES = [
    (mode, template.format(mode=mode_text))
    for mode, mode_texts in MODE_SPELLINGS.items()
    for mode_text in mode_texts
    for template in DENIAL_TEMPLATES
]


def test_a_denied_sole_work_mode_is_never_eligible():
    failures = [
        posting
        for mode, posting in SOLE_DENIAL_CASES
        if Location(mode, "").accepts(Location.parse(posting))
    ]
    assert not failures, report(failures, len(SOLE_DENIAL_CASES))


def test_a_denied_sole_work_mode_never_infers_another_mode():
    """Denying the only stated mode leaves nothing stated, never its opposite."""
    failures = [
        posting
        for _, posting in SOLE_DENIAL_CASES
        if Location.parse(posting).work_mode not in (UNKNOWN, AMBIGUOUS)
    ]
    assert not failures, report(failures, len(SOLE_DENIAL_CASES))


# --- Invariant 3: adding a denial never widens what is offered ---------------------------

BASE_POSTINGS = [
    text if region_text is None else f"{text} {region_text}"
    for texts in MODE_SPELLINGS.values()
    for text in texts
    for region_text in (None, "US", "Canada")
]
APPENDED_DENIALS = [
    template.format(mode=mode_text)
    for mode_texts in MODE_SPELLINGS.values()
    for mode_text in mode_texts[:1]
    for template in DENIAL_TEMPLATES
] + [
    template.format(region=region) for region in ("US", "Canada") for template in REGION_EXCLUSIONS
]
WIDENING_CASES = [
    (base, joined(base, denial, joiner))
    for base in BASE_POSTINGS
    for denial in APPENDED_DENIALS
    for joiner in (", ", " - ")
]


def test_adding_a_denial_never_makes_a_posting_newly_eligible():
    """A denial can only ever subtract. Anything it lets through was already accepted."""
    profiles = tuple(every_profile())
    failures = []
    for base, posting in WIDENING_CASES:
        before = {p for p in profiles if p.accepts(Location.parse(base))}
        after = {p for p in profiles if p.accepts(Location.parse(posting))}
        if not after <= before:
            failures.append((posting, sorted(p.describe() for p in after - before)))
    assert not failures, report(failures, len(WIDENING_CASES))


def test_adding_a_denial_never_introduces_a_mode_that_was_not_there():
    failures = [
        posting for base, posting in WIDENING_CASES if not modes_of(posting) <= modes_of(base)
    ]
    assert not failures, report(failures, len(WIDENING_CASES))


# --- Invariant 4: a denial on one mode never removes another that is offered -------------

CROSS_DENIAL_CASES = [
    (offered, joined(offered_text, template.format(mode=denied_text), joiner))
    for offered, denied in itertools.permutations(MODES, 2)
    for offered_text in MODE_SPELLINGS[offered]
    for denied_text in MODE_SPELLINGS[denied]
    for template in ("no {mode}", "{mode} not available", "no option for {mode}")
    for joiner in (", ", " only, ", " (")
]


def test_a_denial_never_reaches_across_the_mode_that_is_offered():
    """Over-negating trades a false positive for a false negative, which is not an upgrade."""
    failures = [
        posting
        for offered, posting in CROSS_DENIAL_CASES
        if not Location(offered, "").accepts(Location.parse(posting))
    ]
    assert not failures, report(failures, len(CROSS_DENIAL_CASES))


# --- Invariant 5: more than one offered mode is never eligible ---------------------------

AMBIGUOUS_CASES = [
    joined(first_text, second_text, joiner)
    for first, second in itertools.combinations(MODES, 2)
    for first_text in MODE_SPELLINGS[first]
    for second_text in MODE_SPELLINGS[second]
    for joiner in (" or ", " / ", " and ", ", ")
]


def test_two_offered_modes_are_never_eligible_for_a_single_mode_profile():
    profiles = tuple(every_profile())
    failures = [
        posting
        for posting in AMBIGUOUS_CASES
        if Location.parse(posting).work_mode != AMBIGUOUS
        or any(profile.accepts(Location.parse(posting)) for profile in profiles)
    ]
    assert not failures, report(failures, len(AMBIGUOUS_CASES))


def test_the_generated_families_are_large_enough_to_be_worth_running():
    """A silently empty family would make every invariant above pass without testing."""
    sizes = {
        "exclusion": len(EXCLUSION_CASES),
        "sole denial": len(SOLE_DENIAL_CASES),
        "widening": len(WIDENING_CASES),
        "cross denial": len(CROSS_DENIAL_CASES),
        "ambiguous": len(AMBIGUOUS_CASES),
    }
    assert all(count > 30 for count in sizes.values()), sizes
    assert sum(sizes.values()) > 2000, sizes
