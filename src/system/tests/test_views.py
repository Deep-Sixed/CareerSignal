"""Rendering only. These tests never open a database."""

import pytest

from system.views import MISSING, coverage, detail, table


def row(**overrides):
    base = {
        "id": "abc",
        "company": "Example Corp",
        "title": "IAM Architect",
        "location": "remote",
        "url": "https://jobs.example.com/1",
        "status": "reviewing",
        "status_changed_at": 1,
        "review": "review-1",
        "coverage": 86,
        "matched_skills": 6,
        "stated_skills": 7,
        "advances": True,
        "eligible": True,
        "scored": True,
        "actionable": True,
    }
    return {**base, **overrides}


COVERAGE_CASES = [
    ({}, "86%"),
    ({"coverage": 0}, "0%"),
    ({"coverage": 100}, "100%"),
    ({"scored": False, "coverage": None}, "not scored"),
    ({"actionable": False, "coverage": None}, "pre-coverage"),
    ({"review": None, "coverage": None, "scored": None, "actionable": None}, MISSING),
]


@pytest.mark.parametrize(("overrides", "expected"), COVERAGE_CASES)
def test_an_absent_number_says_why_rather_than_leaving_a_blank(overrides, expected):
    assert coverage(row(**overrides)) == expected


def test_an_empty_result_says_so_rather_than_printing_a_bare_header():
    assert table([]) == "No opportunities match."


def test_columns_align_and_no_line_carries_trailing_whitespace():
    rendered = table([row(), row(company="Contoso", title="A", coverage=7, eligible=False)])
    lines = rendered.splitlines()
    assert lines[0].split() == ["STATUS", "COMPANY", "TITLE", "COVERAGE", "ELIGIBLE"]
    assert len(lines) == 3
    for line in lines:
        assert line == line.rstrip(), repr(line)
    # Every column starts at the same offset on every row, header included.
    for header, first, second in (
        ("COMPANY", "Example Corp", "Contoso"),
        ("TITLE", "IAM Architect", "A"),
        ("COVERAGE", "86%", "7%"),
        ("ELIGIBLE", "yes", "no"),
    ):
        offsets = {lines[0].index(header), lines[1].index(first), lines[2].index(second)}
        assert len(offsets) == 1, (header, offsets)


def test_a_long_title_is_never_truncated():
    long = "Principal Identity and Access Management Architect, Platform Engineering Group"
    rendered = table([row(title=long)])
    assert long in rendered


def test_the_table_is_plain_ascii_on_every_console():
    rendered = table([row(), row(coverage=None, scored=False, eligible=False)])
    rendered.encode("ascii")
    assert "yes" in rendered and "no" in rendered


def test_booleans_read_as_words_and_missing_values_as_a_dash():
    rendered = table([row(eligible=None, status=None)])
    assert f"{MISSING}" in rendered


def test_the_detail_view_names_what_it_shows():
    record = {
        **row(),
        "packet": {"reasons": ["Stated skill coverage: 6/7 (86%)", "Advances"], "draft": "Hello"},
        "history": [
            {"status": "new", "actor": "intake", "reason": "", "created_at": 1},
            {"status": "reviewing", "actor": "operator", "reason": "Worth a look", "created_at": 2},
        ],
    }
    rendered = detail(record)
    rendered.encode("ascii")
    for expected in (
        "Example Corp - IAM Architect",
        "https://jobs.example.com/1",
        "coverage   86%",
        "eligible   yes",
        "Stated skill coverage: 6/7 (86%)",
        "Hello",
        "Worth a look",
        "intake",
    ):
        assert expected in rendered, expected


def test_the_detail_view_survives_an_opportunity_with_no_review():
    record = {
        **row(
            review=None,
            coverage=None,
            scored=None,
            actionable=None,
            eligible=None,
            advances=None,
            status=None,
        ),
        "packet": None,
        "history": [],
    }
    rendered = detail(record)
    assert "no current review" in rendered
