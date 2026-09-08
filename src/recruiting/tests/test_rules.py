import pytest

from recruiting.models import Opportunity, Profile, canonical_url, evaluate


def job(**changes):
    value = dict(
        title=" Engineer ",
        company="Example Company",
        url="https://jobs.example.com/1",
        location="REMOTE",
        skills=["Python", "SQL"],
    )
    value.update(changes)
    return Opportunity.normalize(value)


def test_normalization_and_tracking_deduplication():
    assert job(url="https://JOBS.example.com/1?utm_source=alert#top").key == job().key
    assert job().title == "Engineer"
    assert (
        job(url="https://jobs.example.com/?id=1").key
        != job(url="https://jobs.example.com/?id=2").key
    )


def test_threshold_is_strict_and_eligibility_overrides_score():
    skills = tuple(f"skill{i}" for i in range(10))
    profile = Profile(skills)
    assert not evaluate(job(skills=list(skills[:7])), profile).advances
    assert evaluate(job(skills=list(skills[:8])), profile).advances
    assert not evaluate(job(location="onsite"), Profile(("python", "sql"))).advances
    assert not evaluate(job(location=""), Profile(("python",))).eligible


def test_missing_skills_and_changed_configuration_are_visible():
    result = evaluate(job(skills=["python"]), Profile(("python", "sql")))
    assert result.score == 50 and "Missing: sql" in result.reasons
    assert result.id != evaluate(job(skills=["python"]), Profile(("python",))).id


@pytest.mark.parametrize(
    "url",
    [
        "http://jobs.example.com/1",
        "file:///private",
        "https://user:pass@example.com",  # pragma: allowlist secret - synthetic URL
    ],
)
def test_reject_unsafe_urls(url):
    with pytest.raises(ValueError):
        job(url=url)


BASE = "https://jobs.example.com/roles/1"


@pytest.mark.parametrize(
    "equivalent",
    [
        "https://jobs.example.com/roles/1/",  # single trailing separator
        "https://jobs.example.com:443/roles/1",  # default port for https
        "https://JOBS.Example.COM/roles/1",  # host case
        "https://www.jobs.example.com/roles/1",  # leading www. label only
        "https://jobs.example.com/roles/1?utm_source=alert&utm_campaign=x",
        "https://jobs.example.com/roles/1?gclid=abc",
        "https://jobs.example.com/roles/1?fbclid=abc",
        "https://jobs.example.com/roles/1?msclkid=abc",
        "https://jobs.example.com/roles/1#top",  # decorative in-page anchor
        "https://jobs.example.com/roles/1#Apply",
    ],
)
def test_equivalent_job_urls_collapse_to_one_key(equivalent):
    assert canonical_url(equivalent) == canonical_url(BASE)


@pytest.mark.parametrize(
    "distinct",
    [
        "https://jobs.example.com/Roles/1",  # path case is the server's to decide
        "https://jobs.example.com/roles/2",
        "https://jobs.example.com:8443/roles/1",  # non-default port
        "https://careers.example.com/roles/1",  # different non-www subdomain
        "https://jobs.example.com/roles/1?id=1",  # unknown parameter is meaningful
        "https://jobs.example.com/roles/1?ref=email",
        "https://jobs.example.com/roles/1#/job/123",  # hash-routed job identity
        "https://jobs.example.com/roles/1//",  # repeated separators can be meaningful
        "https://jobs.example.com/roles/1///",
    ],
)
def test_distinct_job_urls_keep_distinct_keys(distinct):
    assert canonical_url(distinct) != canonical_url(BASE)


def test_hash_routed_jobs_are_not_merged_with_each_other():
    """Discarding fragments outright would silently collapse different jobs into one."""
    first = "https://jobs.example.com/careers#/job/123"
    second = "https://jobs.example.com/careers#/job/456"
    assert canonical_url(first) != canonical_url(second)
    # A tracking parameter is still stripped around a preserved fragment.
    tracked = "https://jobs.example.com/careers?utm_source=alert#/job/123"
    assert canonical_url(tracked) == canonical_url(first)


def test_semantic_query_values_and_ordering():
    assert canonical_url(BASE + "?id=1") != canonical_url(BASE + "?id=2")
    assert canonical_url(BASE + "?a=1&b=2") == canonical_url(BASE + "?b=2&a=1")
    # A stripped tracking parameter must not take a meaningful one with it.
    assert canonical_url(BASE + "?id=1&utm_source=alert") == canonical_url(BASE + "?id=1")


def test_canonicalization_reaches_the_deduplication_key():
    for equivalent in ("https://www.jobs.example.com/roles/1/?utm_source=alert#top",):
        assert job(url=equivalent).key == job(url=BASE).key
    assert job(url="https://jobs.example.com/Roles/1").key != job(url=BASE).key


def test_only_a_single_trailing_separator_is_normalized():
    """Dropping every trailing separator would be the false collapse this PR argues against."""
    assert canonical_url(BASE + "/") == canonical_url(BASE)
    assert canonical_url(BASE + "//") != canonical_url(BASE)
    assert canonical_url(BASE + "//") != canonical_url(BASE + "/")
    # A repeated separator survives canonicalization rather than being quietly trimmed.
    assert canonical_url(BASE + "//").endswith("/roles/1//")
    # The root path keeps its only separator.
    assert canonical_url("https://jobs.example.com/") == "https://jobs.example.com/"
    assert canonical_url("https://jobs.example.com") == "https://jobs.example.com/"


REMOTE_PROFILE = Profile(("python", "sql"), ("remote",))
US_REMOTE_PROFILE = Profile(("python", "sql"), ("remote US",))


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("remote", True),
        ("Remote", True),
        ("REMOTE", True),
        ("remote - US", True),
        ("remote (US)", True),
        ("US remote", True),
        ("fully remote", True),
        ("100% remote", True),
        ("remote work", True),
        ("remote Canada", True),  # the profile states no region, so it does not exclude one
        ("hybrid", False),
        ("onsite", False),
        ("Philadelphia", False),
        ("New York", False),
        ("", False),
    ],
)
def test_eligibility_reads_structure_not_spelling(location, expected):
    assert evaluate(job(location=location), REMOTE_PROFILE).eligible is expected


@pytest.mark.parametrize(
    ("location", "expected"),
    [("remote US", True), ("remote", True), ("remote Canada", False), ("remote Europe", False)],
)
def test_a_configured_region_excludes_other_regions(location, expected):
    assert evaluate(job(location=location), US_REMOTE_PROFILE).eligible is expected


def test_reasons_name_the_structure_eligibility_used():
    reasons = evaluate(job(location="Remote (US)"), REMOTE_PROFILE).reasons
    assert "Location read as remote / us" in reasons
    assert "Location eligible" in reasons
    unstated = evaluate(job(location="Philadelphia"), REMOTE_PROFILE).reasons
    assert "Location read as work mode not stated / philadelphia" in unstated
    assert any("work mode not stated" in r for r in unstated[-2:])
    assert "Location missing" in evaluate(job(location=""), REMOTE_PROFILE).reasons


def test_unusable_location_configuration_fails_loudly():
    """A profile that could never match anything is a configuration error, not a silent zero."""
    with pytest.raises(ValueError, match="must state a work mode"):
        Profile(("python",), ("Philadelphia",))
    with pytest.raises(ValueError, match="At least one accepted location"):
        Profile(("python",), ())
    # Stating the mode makes the same place usable.
    assert Profile(("python",), ("onsite Philadelphia",)).accepted_locations[0].region == (
        "philadelphia"
    )


@pytest.mark.parametrize(
    "location",
    [
        "not remote",
        "no remote",
        "hybrid - not remote",
        "onsite only - no remote",
        "onsite (no remote option)",
        "remote or hybrid",
    ],
)
def test_negated_or_ambiguous_postings_are_not_eligible_end_to_end(location):
    review = evaluate(job(location=location), REMOTE_PROFILE)
    assert review.eligible is False and review.advances is False


def test_reasons_separate_ambiguity_from_a_missing_location():
    ambiguous = evaluate(job(location="remote or hybrid"), REMOTE_PROFILE).reasons
    assert "Location read as more than one work mode stated" in ambiguous
    assert "Location states more than one work mode; not resolved automatically" in ambiguous
    assert "Location missing" not in ambiguous


def test_a_profile_cannot_be_configured_with_an_ambiguous_location():
    with pytest.raises(ValueError, match="must state a work mode"):
        Profile(("python",), ("remote or hybrid",))


@pytest.mark.parametrize(
    ("location", "configured"),
    [
        ("remote, not Canada", "remote"),
        ("onsite (no remote option)", "onsite"),
        ("hybrid, remote not available", "hybrid"),
    ],
)
def test_a_denial_elsewhere_still_leaves_the_job_eligible(location, configured):
    profile = Profile(("python", "sql"), (configured,))
    assert evaluate(job(location=location), profile).eligible is True


def test_a_posting_excluding_the_configured_region_is_not_eligible():
    canada = Profile(("python", "sql"), ("remote Canada",))
    assert evaluate(job(location="remote, not Canada"), canada).eligible is False
    assert evaluate(job(location="remote Canada"), canada).eligible is True
    # The same posting still satisfies an unconstrained remote profile.
    assert evaluate(job(location="remote, not Canada"), REMOTE_PROFILE).eligible is True
    reasons = evaluate(job(location="remote, not Canada"), canada).reasons
    assert "Location read as remote, region not stated, excluding canada" in reasons


def test_a_profile_cannot_be_configured_with_an_excluded_region():
    with pytest.raises(ValueError, match="cannot exclude a region"):
        Profile(("python",), ("remote not Canada",))
