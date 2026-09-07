import pytest

from recruiting.models import Opportunity, Profile, evaluate


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
