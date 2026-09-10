"""Rendering only. These tests never open a database."""

import re

import pytest

from system.views import CONTROL, MISSING, coverage, detail, safe, safe_lines, table

# Every character a terminal may act on, not the handful that came to mind.
CONTROLS = (
    [chr(code) for code in range(0x00, 0x20)]
    + [chr(0x7F)]
    + [chr(code) for code in range(0x80, 0xA0)]
)
# Sequences a real terminal would execute, if any of these reached it.
ATTACKS = [
    "Engineer\x1b]0;spoofed\x07",
    "Engineer\x1b[2J",
    "Engineer\x1b[31mred",
    "Engineer\x1b]8;;https://evil.example.com\x07click\x1b]8;;\x07",
    "Engineer\x08\x08\x08\x08hidden",
    "Engineer\rOverwritten",
    "Engineer\x9b2J",
    "Engineer\x7fdelete",
    "Engineer\x00null",
]


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
        "status_event": 12,
        "action": {"decision": None, "actor": None, "draft": "none", "receipt": None},
        "bound": {
            "review": "review-1",
            "source": "message-1",
            "to": "recruiter@example.com",
            "subject": "A role for you",
            "wording": "Hello",
        },
    }
    return {**base, **overrides}


def acted(**overrides):
    """The approval and draft state a detail view reports."""
    return {"action": {**row()["action"], **overrides}}


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


def test_the_table_chrome_is_plain_ascii():
    """The headers, padding and markers are ASCII; the data is whatever the mailbox sent."""
    rendered = table([row(), row(coverage=None, scored=False, eligible=False)])
    rendered.encode("ascii")
    assert "yes" in rendered and "no" in rendered


@pytest.mark.parametrize("character", CONTROLS)
def test_every_control_character_is_escaped_rather_than_printed(character):
    escaped = safe(f"before{character}after")
    assert character not in escaped
    assert escaped == f"before\\x{ord(character):02x}after"


@pytest.mark.parametrize(
    "text", ["Zürich Söhne", "東京", "Ivan Kovačević", "naïve café", "Ω≈ç√", "İstanbul"]
)
def test_ordinary_unicode_survives_untouched(text):
    """Safety is not achieved by flattening everything to ASCII."""
    assert safe(text) == text


@pytest.mark.parametrize("attack", ATTACKS)
def test_no_attack_sequence_survives_into_a_table(attack):
    rendered = table([row(title=attack, company=attack, status=attack)])
    assert not CONTROL.search(rendered.replace("\n", ""))
    # The escape is visible rather than silently dropped, so the operator can see it.
    assert "\\x1b" in rendered or "\\x" in rendered


@pytest.mark.parametrize("attack", ATTACKS)
def test_no_attack_sequence_survives_into_a_detail_view(attack):
    record = {
        **row(
            title=attack,
            company=attack,
            location=attack,
            url=attack,
            bound={
                "review": "review-1",
                "source": attack,
                "to": attack,
                "subject": attack,
                "wording": attack,
            },
        ),
        "packet": {"reasons": [attack], "draft": attack},
        "history": [
            {"status": "new", "actor": attack, "reason": attack, "created_at": 1, "event": 1}
        ],
    }
    assert not CONTROL.search(detail(record).replace("\n", ""))


def test_a_newline_stays_structure_while_everything_else_escapes():
    assert safe_lines("line one\nline two\x1b[31m") == ["line one", "line two\\x1b[31m"]
    assert safe_lines("") == [""]
    assert safe_lines("only") == ["only"]
    assert safe_lines("one\r\ntwo") == ["one", "two"]
    assert safe_lines("one\n") == ["one", ""]


# Everything str.splitlines() treats as a line boundary. Only LF, and CRLF folded into it,
# is structural here; the rest must stay in the line so safe() can render them visibly.
SPLITLINES_BOUNDARIES = [
    chr(code) for code in (0x0B, 0x0C, 0x0D, 0x1C, 0x1D, 0x1E, 0x85, 0x2028, 0x2029)
]


@pytest.mark.parametrize("character", SPLITLINES_BOUNDARIES)
def test_only_a_line_feed_is_structural(character):
    """splitlines() would swallow these, turning a hostile control into invisible structure."""
    rendered = safe_lines(f"one{character}two")
    assert len(rendered) == 1, (hex(ord(character)), rendered)
    assert "one" in rendered[0] and "two" in rendered[0]


@pytest.mark.parametrize("character", [c for c in SPLITLINES_BOUNDARIES if CONTROL.match(c)])
def test_a_swallowed_control_character_is_escaped_visibly(character):
    assert safe_lines(f"one{character}two") == [f"one\\x{ord(character):02x}two"]


@pytest.mark.parametrize("attack", ATTACKS)
def test_no_attack_sequence_survives_a_multiline_path(attack):
    """The detail view's draft and history reasons go through safe_lines, not safe."""
    for line in safe_lines(attack):
        assert not CONTROL.search(line), (attack, repr(line))
    record = {
        **row(bound={**row()["bound"], "wording": f"first\n{attack}\nlast"}),
        "packet": {"reasons": ["ok"], "draft": f"first\n{attack}\nlast"},
        "history": [
            {
                "status": "new",
                "actor": "operator",
                "reason": f"one\n{attack}",
                "created_at": 1,
                "event": 1,
            }
        ],
    }
    rendered = detail(record)
    for line in rendered.splitlines():
        assert not CONTROL.search(line), (attack, repr(line))
    assert "first" in rendered and "last" in rendered


def test_a_bare_carriage_return_cannot_overwrite_a_rendered_line():
    """\\r would return the cursor and let later text overwrite what was already printed."""
    assert safe_lines("visible\rhidden") == ["visible\\x0dhidden"]
    record = {
        **row(bound={**row()["bound"], "wording": "visible\rhidden"}),
        "packet": {"reasons": ["ok"], "draft": "visible\rhidden"},
        "history": [
            {
                "status": "new",
                "actor": "operator",
                "reason": "visible\rhidden",
                "created_at": 1,
                "event": 1,
            }
        ],
    }
    rendered = detail(record)
    assert "visible" in rendered and "hidden" in rendered
    assert "\r" not in rendered


def test_a_multi_line_operator_reason_is_still_readable():
    record = {
        **row(bound=None),
        "packet": None,
        "history": [
            {
                "status": "rejected",
                "actor": "operator",
                "reason": "Wrong team\nRevisit next quarter",
                "created_at": 1,
                "event": 1,
            }
        ],
    }
    rendered = detail(record)
    assert "Wrong team" in rendered and "Revisit next quarter" in rendered
    assert not CONTROL.search(rendered.replace("\n", ""))
    for line in rendered.splitlines():
        assert not re.search(r"[\x00-\x1f\x7f-\x9f]", line), repr(line)


def test_booleans_read_as_words_and_missing_values_as_a_dash():
    rendered = table([row(eligible=None, status=None)])
    assert f"{MISSING}" in rendered


def test_the_detail_view_names_what_it_shows():
    record = {
        **row(),
        "packet": {"reasons": ["Stated skill coverage: 6/7 (86%)", "Advances"], "draft": "Hello"},
        "history": [
            {"status": "new", "actor": "intake", "reason": "", "created_at": 1, "event": 1},
            {
                "status": "reviewing",
                "actor": "operator",
                "reason": "Worth a look",
                "created_at": 2,
                "event": 2,
            },
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
            bound=None,
        ),
        "packet": None,
        "history": [],
    }
    rendered = detail(record)
    assert "no current review" in rendered
