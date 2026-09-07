from recruiting.extraction import extract


def test_recruiter_labeled_message_and_missing_optional_fields():
    items = extract(
        "Hello candidate,\nRole: Engineer\nCompany: Example Company\n"
        "URL: https://jobs.example.com/1\nThanks!"
    )
    assert len(items) == 1 and items[0].opportunity
    assert items[0].opportunity.location == ""
    assert items[0].opportunity.skills == ()


def test_independent_jobs_and_partial_errors():
    items = extract(
        "Title: One\nCompany: Example Company\nURL: https://jobs.example.com/1\n"
        "Title: Broken\nLocation: remote\n"
        "Title: Two\nCompany: Example Company\nURL: https://jobs.example.com/2\n"
        "Skills: Python; SQL, Python"
    )
    assert len(items) == 3
    assert items[0].opportunity and items[2].opportunity
    assert items[1].opportunity is None and "company, url" in items[1].reason
    assert items[2].opportunity.skills == ("python", "sql")


def test_conflicting_values_and_unsupported_content_are_reviewable():
    assert "Conflicting fields" in extract("Role: One\nCompany: First\nCompany: Second")[0].reason
    assert "No supported" in extract("An exciting role might fit you. Reply for details!")[0].reason
    assert extract("Role: One\nCompany: Example\nURL: javascript:alert(1)")[0].opportunity is None


def test_conflicting_versions_reject_both_without_discarding_other_jobs():
    items = extract(
        "Title: One\nCompany: Example\nURL: https://jobs.example.com/1\n"
        "Title: Changed\nCompany: Example\nURL: https://jobs.example.com/1\n"
        "Title: Good\nCompany: Example\nURL: https://jobs.example.com/2"
    )
    assert items[0].opportunity is None and items[1].opportunity is None
    assert items[2].opportunity
