from recruiting.extraction import extract, extract_records


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


def record(index, **changes):
    value = dict(
        title=f"Engineer {index}",
        company="Example Company",
        url=f"https://jobs.example.com/{index}",
        location="remote",
        skills=["python"],
    )
    value.update(changes)
    return value


def test_one_malformed_record_never_discards_its_siblings():
    """The whole message used to abort on the first bad entry."""
    items = extract_records(
        [
            record(1),
            record(2, url="http://insecure.example.com/2"),
            record(3),
            {"company": "Example Company", "url": "https://jobs.example.com/4"},
            "not an object",
            record(5),
        ]
    )
    assert len(items) == 6
    assert [bool(item.opportunity) for item in items] == [True, False, True, False, False, True]
    # Every rejected entry carries a reason, and every accepted one carries none.
    for item in items:
        assert bool(item.reason) is (item.opportunity is None)
    assert items[4].reason == "Job entry must be an object"


def test_every_record_keeps_its_position():
    items = extract_records([record(1, title=""), record(2)])
    assert [item.index for item in items] == [0, 1]
    assert items[1].opportunity.title == "Engineer 2"


def test_conflicting_versions_reject_both_records_without_touching_others():
    items = extract_records([record(1), record(1, title="Changed"), record(2)])
    assert items[0].opportunity is None and items[1].opportunity is None
    assert "Conflicting versions" in items[0].reason
    assert items[2].opportunity is not None


def test_identical_duplicates_are_not_a_conflict():
    items = extract_records([record(1), record(1)])
    assert all(item.opportunity for item in items)
    assert items[0].opportunity == items[1].opportunity
