"""The operator query commands report; they must never change anything."""

import json
import re

import pytest

from communications.controlled import ControlledDrafts
from data import store
from data.repository import Repository
from recruiting.models import Profile
from system import cli
from system.workflow import Workflow

TABLES = (
    "messages",
    "opportunities",
    "reviews",
    "provenance",
    "decisions",
    "draft_intents",
    "extraction_items",
    "audit",
    "opportunity_status_history",
    "schema_migrations",
)
BODY = json.dumps(
    {
        "jobs": [
            {
                "title": "IAM Architect",
                "company": "Example Corp",
                "url": "https://jobs.example.com/1",
                "location": "remote",
                "skills": ["Python", "SQL"],
            },
            {
                "title": "Desktop Support",
                "company": "Fabrikam",
                "url": "https://jobs.example.com/2",
                "location": "onsite New York",
                "skills": ["Windows", "Python"],
            },
        ]
    }
)


def prepared(path):
    repository = Repository(path)
    flow = Workflow(repository, ControlledDrafts(), Profile(("python", "sql")))
    reviews = flow.intake("message-1", BODY)
    repository.decide(reviews[0], approved=True, actor="operator")
    flow.draft(reviews[0])
    for row, status in zip(repository.opportunities(), ("reviewing", "rejected")):
        repository.record_status(row["id"], status, actor="operator", reason="triage")
    return repository


def snapshot(path):
    with store.connection(path) as conn:
        return {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in TABLES}


def run(monkeypatch, capsys, *arguments):
    monkeypatch.setattr("sys.argv", ["careersignal", *arguments])
    cli.main()
    return capsys.readouterr().out


def refusal(monkeypatch, capsys, *arguments):
    """The exit code is 2 for every argparse refusal, so the message is what distinguishes
    one from another. Asserting only the code would let any guard be removed unnoticed."""
    with pytest.raises(SystemExit) as stopped:
        run(monkeypatch, capsys, *arguments)
    assert stopped.value.code == 2, arguments
    return capsys.readouterr().err


COMMANDS = [
    ("opportunities",),
    ("opportunities", "--json"),
    ("opportunities", "--active"),
    ("opportunities", "--status", "reviewing"),
    ("opportunities", "--status", "closed"),
    ("opportunities", "--eligible"),
    ("opportunities", "--ineligible"),
    ("opportunities", "--min-coverage", "50"),
    ("opportunities", "--max-coverage", "50"),
    ("opportunities", "--min-coverage", "0", "--max-coverage", "100", "--json"),
]


def test_no_query_command_changes_anything(tmp_path, monkeypatch, capsys):
    path = tmp_path / "db"
    repository = prepared(path)
    identifier = repository.opportunities()[0]["id"]
    before = snapshot(path)
    for command in COMMANDS:
        run(monkeypatch, capsys, *command, "--db", str(path))
    for extra in ((), ("--json",)):
        run(monkeypatch, capsys, "opportunity", identifier, *extra, "--db", str(path))
    assert snapshot(path) == before


def test_the_table_is_the_default_and_json_is_behind_the_flag(tmp_path, monkeypatch, capsys):
    path = tmp_path / "db"
    prepared(path)
    printed = run(monkeypatch, capsys, "opportunities", "--db", str(path))
    assert printed.splitlines()[0].split() == [
        "STATUS",
        "COMPANY",
        "TITLE",
        "COVERAGE",
        "ELIGIBLE",
    ]
    with pytest.raises(json.JSONDecodeError):
        json.loads(printed)
    structured = json.loads(run(monkeypatch, capsys, "opportunities", "--json", "--db", str(path)))
    assert {row["company"] for row in structured} == {"Example Corp", "Fabrikam"}
    assert {"id", "url", "location", "status_changed_at"} <= set(structured[0])


def test_the_json_form_carries_what_the_table_leaves_out(tmp_path, monkeypatch, capsys):
    """The table is deliberately five columns; automation must still see everything."""
    path = tmp_path / "db"
    prepared(path)
    printed = run(monkeypatch, capsys, "opportunities", "--db", str(path))
    structured = json.loads(run(monkeypatch, capsys, "opportunities", "--json", "--db", str(path)))
    assert structured[0]["url"] not in printed
    assert str(structured[0]["status_changed_at"]) not in printed


def test_a_filter_that_cannot_mean_anything_stops_the_command(tmp_path, monkeypatch, capsys):
    path = tmp_path / "db"
    prepared(path)
    for arguments, expected in (
        (("opportunities", "--status", "reopened"), "Unknown status"),
        (("opportunities", "--min-coverage", "101"), "min_coverage must be"),
        (("opportunities", "--max-coverage", "-1"), "max_coverage must be"),
        (("opportunities", "--eligible", "--ineligible"), "cannot both be given"),
        (("opportunity",), "requires an opportunity id"),
        (("opportunity", "no-such-opportunity"), "No opportunity with id"),
    ):
        printed = refusal(monkeypatch, capsys, *arguments, "--db", str(path))
        assert expected in printed, (arguments, printed)


# Control characters survive recruiting.models.clean, which collapses whitespace only, so
# a hostile title reaches the terminal unless presentation escapes it.
ESC, BEL = chr(0x1B), chr(0x07)
HOSTILE_TITLE = f"Engineer{ESC}]0;spoofed{BEL}"
HOSTILE_COMPANY = f"Z\u00fcrich S\u00f6hne{ESC}[2J"
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
HOSTILE_BODY = json.dumps(
    {
        "jobs": [
            {
                "title": HOSTILE_TITLE,
                "company": HOSTILE_COMPANY,
                "url": "https://jobs.example.com/9",
                "location": f"remote{ESC}[31m",
                "skills": ["Python", "SQL"],
            }
        ]
    }
)


def hostile(path):
    repository = Repository(path)
    flow = Workflow(repository, ControlledDrafts(), Profile(("python", "sql")))
    flow.intake("hostile-message", HOSTILE_BODY)
    identifier = repository.opportunities()[0]["id"]
    repository.record_status(
        identifier,
        "reviewing",
        actor="operator",
        reason=f"line one\nline two{ESC}[31m",
    )
    return repository, identifier


def test_a_hostile_title_reaches_storage_unchanged(tmp_path):
    """The premise of the tests below: normalization does not remove these characters."""
    repository, identifier = hostile(tmp_path / "db")
    assert repository.opportunity(identifier)["title"] == HOSTILE_TITLE
    assert ESC in repository.opportunity(identifier)["title"]


def test_no_control_sequence_reaches_the_terminal_from_an_ingested_opportunity(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "db"
    _, identifier = hostile(path)
    for arguments in (
        ("opportunities",),
        ("opportunities", "--status", "reviewing"),
        ("opportunity", identifier),
    ):
        printed = run(monkeypatch, capsys, *arguments, "--db", str(path))
        assert printed, arguments
        for line in printed.splitlines():
            assert not CONTROL.search(line), (arguments, repr(line))


def test_the_escaped_form_is_visible_and_the_unicode_is_not_flattened(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "db"
    hostile(path)
    printed = run(monkeypatch, capsys, "opportunities", "--db", str(path))
    assert "\\x1b" in printed and "\\x07" in printed
    assert "Z\u00fcrich S\u00f6hne" in printed


def test_a_multi_line_reason_survives_the_detail_view_one_safe_line_at_a_time(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "db"
    _, identifier = hostile(path)
    printed = run(monkeypatch, capsys, "opportunity", identifier, "--db", str(path))
    assert "line one" in printed and "line two" in printed
    for line in printed.splitlines():
        assert not CONTROL.search(line), repr(line)


def test_the_json_form_keeps_the_underlying_data_escaped_by_json(tmp_path, monkeypatch, capsys):
    """JSON needs no help: json.dumps escapes controls, and the raw value must survive."""
    path = tmp_path / "db"
    _, identifier = hostile(path)
    printed = run(monkeypatch, capsys, "opportunities", "--json", "--db", str(path))
    assert ESC not in printed and BEL not in printed
    assert "\\u001b" in printed
    assert json.loads(printed)[0]["title"] == HOSTILE_TITLE
    record = json.loads(
        run(monkeypatch, capsys, "opportunity", identifier, "--json", "--db", str(path))
    )
    assert record["title"] == HOSTILE_TITLE
    assert record["history"][-1]["reason"] == f"line one\nline two{ESC}[31m"


def test_the_detail_command_shows_the_packet_and_the_history(tmp_path, monkeypatch, capsys):
    path = tmp_path / "db"
    repository = prepared(path)
    identifier = repository.opportunities()[0]["id"]
    printed = run(monkeypatch, capsys, "opportunity", identifier, "--db", str(path))
    assert "Example Corp - IAM Architect" in printed
    assert "Stated skill coverage" in printed
    assert "reviewing" in printed and "triage" in printed
    assert "intake" in printed


def test_queries_never_reach_a_mailbox(tmp_path, monkeypatch, capsys):
    """No query path may construct a Gmail reader, let alone contact one."""
    path = tmp_path / "db"
    prepared(path)

    def refuse(*_args, **_kwargs):
        raise AssertionError("a query command touched the Gmail adapter")

    monkeypatch.setattr("communications.gmail.GmailReader.__init__", refuse)
    monkeypatch.setattr("communications.gmail.https_get", refuse)
    for command in COMMANDS:
        run(monkeypatch, capsys, *command, "--db", str(path))
