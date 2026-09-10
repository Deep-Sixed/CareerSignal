"""The operator interface is a boundary over what the application already does.

Every test here asks one of two questions. Does a command that reports change anything?
Does a command that acts go through the machinery that owns the decision, rather than
around it? Nothing in this surface may invent an outcome, and nothing may collapse two
outcomes that call for different moves -- a refusal, where nothing left this machine, and
an uncertain result, where something may have.
"""

import json
import pathlib
import re

import pytest

from communications import gmail, gmail_draft
from communications.controlled import ControlledDrafts
from communications.gmail_draft import COMPOSE_TOKEN_VARIABLE
from communications.message import Message
from data import store
from data.repository import Repository
from recruiting.models import Profile
from system import cli
from system.workflow import Workflow

TOKEN = "synthetic-compose-token-value"
MAILBOX = "operator@example.com"
HOSTILE_SENDER = "recruiter@example.com\r\nBcc: attacker@example.com"
ESC = chr(0x1B)
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
JOB_TEXT = (
    "Title: IAM Architect\r\n"
    "Company: Example Corp\r\n"
    "Location: remote\r\n"
    "Skills: Python, SQL\r\n"
    "URL: https://jobs.example.com/roles/1\r\n"
)
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
    "message_sources",
    "schema_migrations",
)


def ingest(path, *, sender="recruiter@example.com"):
    workflow = Workflow(Repository(path), ControlledDrafts(), Profile(("python", "sql")))
    review = workflow.intake_message(
        Message(
            namespace="gmail:operator@example.com",
            external_id="m1",
            sender=sender,
            subject="A role for you",
            text=JOB_TEXT,
        )
    )[0]
    return workflow, review, workflow.repository.opportunities()[0]["id"]


def snapshot(path):
    with store.connection(path) as conn:
        return {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in TABLES}


def run(monkeypatch, capsys, *arguments):
    monkeypatch.setattr("sys.argv", ["careersignal", *arguments])
    cli.main()
    return capsys.readouterr().out


def stopped(monkeypatch, capsys, code, *arguments):
    """Run a command that does not exit 0 and return everything it printed."""
    with pytest.raises(SystemExit) as exit_code:
        run(monkeypatch, capsys, *arguments)
    assert exit_code.value.code == code, (arguments, exit_code.value.code)
    printed = capsys.readouterr()
    return printed.out, printed.err


def event_of(monkeypatch, capsys, path, opportunity):
    """Read the current status event the way an operator would: from the interface."""
    detail = json.loads(
        run(monkeypatch, capsys, "opportunity", opportunity, "--json", "--db", str(path))
    )
    return detail["status_event"]


# --- reading changes nothing ---------------------------------------------------------------


READS = [
    ("opportunities",),
    ("opportunities", "--json"),
    ("opportunities", "--active"),
    ("opportunities", "--status", "reviewing"),
]


def test_no_reporting_command_writes_anything_or_builds_a_provider(tmp_path, monkeypatch, capsys):
    """Including the detail view now that it reports approval and draft state.

    Reading what was decided must not be able to decide anything, and it must not be able
    to reach a mailbox in order to find out.
    """
    path = tmp_path / "db"
    workflow, review, opportunity = ingest(path)
    workflow.repository.decide(review, approved=True, actor="operator")
    workflow.draft(review)
    workflow.repository.record_status(
        opportunity, "interested", actor="operator", reason="worth a look"
    )
    before = snapshot(path)

    def refuse(*args, **kwargs):
        raise AssertionError("a reporting command built a draft or mailbox client")

    monkeypatch.setattr(gmail_draft.GmailDrafts, "__init__", refuse)
    monkeypatch.setattr(gmail.GmailReader, "__init__", refuse)
    monkeypatch.setattr(ControlledDrafts, "__init__", refuse)
    for command in READS:
        run(monkeypatch, capsys, *command, "--db", str(path))
    for extra in ((), ("--json",)):
        run(monkeypatch, capsys, "opportunity", opportunity, *extra, "--db", str(path))
    assert snapshot(path) == before


# --- recording a status ---------------------------------------------------------------------


def test_a_status_is_recorded_against_the_event_the_operator_read(tmp_path, monkeypatch, capsys):
    path = tmp_path / "db"
    _, _, opportunity = ingest(path)
    read = event_of(monkeypatch, capsys, path, opportunity)
    printed = run(
        monkeypatch,
        capsys,
        "status",
        opportunity,
        "--to",
        "interested",
        "--expect",
        str(read),
        "--actor",
        "operator",
        "--reason",
        "worth a look",
        "--db",
        str(path),
    )
    assert printed.startswith("ACCEPTED")
    assert "interested" in printed
    structured = json.loads(
        run(
            monkeypatch,
            capsys,
            "opportunity",
            opportunity,
            "--json",
            "--db",
            str(path),
        )
    )
    assert structured["status"] == "interested"
    assert structured["status_event"] > read
    assert structured["history"][-1]["reason"] == "worth a look"


def test_a_status_written_against_a_stale_event_is_refused(tmp_path, monkeypatch, capsys):
    """The whole point: a decision taken against a state that has since moved."""
    path = tmp_path / "db"
    _, _, opportunity = ingest(path)
    read = event_of(monkeypatch, capsys, path, opportunity)
    # Something else moves the opportunity after the operator read it.
    Repository(path).record_status(
        opportunity, "withdrawn", actor="other", reason="filled", expected_event_id=read
    )
    before = snapshot(path)
    out, err = stopped(
        monkeypatch,
        capsys,
        1,
        "status",
        opportunity,
        "--to",
        "applied",
        "--expect",
        str(read),
        "--actor",
        "operator",
        "--db",
        str(path),
    )
    assert out.startswith("REFUSED")
    assert str(read) in out and "withdrawn" in out
    assert err == "", "an outcome is the answer, not a diagnostic"
    assert snapshot(path) == before
    assert Repository(path).status(opportunity) == "withdrawn"


def test_the_refusal_is_structured_for_a_script_too(tmp_path, monkeypatch, capsys):
    path = tmp_path / "db"
    _, _, opportunity = ingest(path)
    read = event_of(monkeypatch, capsys, path, opportunity)
    Repository(path).record_status(opportunity, "rejected", actor="other", expected_event_id=read)
    out, _ = stopped(
        monkeypatch,
        capsys,
        1,
        "status",
        opportunity,
        "--to",
        "applied",
        "--expect",
        str(read),
        "--actor",
        "operator",
        "--json",
        "--db",
        str(path),
    )
    record = json.loads(out)
    assert record["outcome"] == "refused"
    assert record["expected_event"] == read
    assert record["event"] > read
    assert record["status"] == "rejected"


@pytest.mark.parametrize(
    "arguments,expected",
    [
        (("status",), "requires an opportunity id"),
        (("status", "job"), "requires --to"),
        (("status", "job", "--to", "applied"), "requires --expect"),
        (("status", "job", "--to", "applied", "--expect", "1"), "requires --actor"),
        (
            ("status", "job", "--to", "applied", "--expect", "1", "--actor", "   "),
            "requires --actor",
        ),
    ],
)
def test_a_status_command_that_cannot_mean_anything_stops_before_the_database(
    tmp_path, monkeypatch, capsys, arguments, expected
):
    """Exit 2 on stderr: the command was written wrongly, which is not the system
    refusing. Collapsing the two would make a typo indistinguishable from a state that
    moved."""
    path = tmp_path / "db"
    ingest(path)
    out, err = stopped(monkeypatch, capsys, 2, *arguments, "--db", str(path))
    assert expected in err
    assert out == ""


def test_an_unknown_status_or_opportunity_is_a_usage_error(tmp_path, monkeypatch, capsys):
    path = tmp_path / "db"
    _, _, opportunity = ingest(path)
    read = event_of(monkeypatch, capsys, path, opportunity)
    common = ("--expect", str(read), "--actor", "operator", "--db", str(path))
    _, err = stopped(monkeypatch, capsys, 2, "status", opportunity, "--to", "reopened", *common)
    assert "Unknown status" in err
    _, err = stopped(monkeypatch, capsys, 2, "status", "no-such-job", "--to", "applied", *common)
    assert "Unknown opportunity" in err


def test_recording_a_status_never_builds_a_provider(tmp_path, monkeypatch, capsys):
    """Changing where an opportunity stands is not an outward action."""
    path = tmp_path / "db"
    _, _, opportunity = ingest(path)
    read = event_of(monkeypatch, capsys, path, opportunity)

    def refuse(*args, **kwargs):
        raise AssertionError("recording a status built a draft or mailbox client")

    monkeypatch.setattr(gmail_draft.GmailDrafts, "__init__", refuse)
    monkeypatch.setattr(gmail.GmailReader, "__init__", refuse)
    monkeypatch.setattr(ControlledDrafts, "__init__", refuse)
    run(
        monkeypatch,
        capsys,
        "status",
        opportunity,
        "--to",
        "applied",
        "--expect",
        str(read),
        "--actor",
        "operator",
        "--db",
        str(path),
    )


# --- refused and uncertain are different states ----------------------------------------------


def approved(path, monkeypatch, capsys, *, sender="recruiter@example.com"):
    workflow, review, opportunity = ingest(path, sender=sender)
    run(monkeypatch, capsys, "approve", review, "--actor", "operator", "--db", str(path))
    return workflow, review, opportunity


def test_a_refused_draft_reports_a_refusal_and_contacts_nothing(tmp_path, monkeypatch, capsys):
    """A recruiter address that cannot become a header. Nothing was created or sent."""
    path = tmp_path / "db"
    _, review, _ = approved(path, monkeypatch, capsys, sender=HOSTILE_SENDER)
    monkeypatch.setenv(COMPOSE_TOKEN_VARIABLE, TOKEN)

    def refuse(*args, **kwargs):
        raise AssertionError("a refused draft still contacted Gmail")

    monkeypatch.setattr(gmail_draft, "_perform", refuse)
    out, err = stopped(
        monkeypatch,
        capsys,
        1,
        "draft",
        review,
        "--provider",
        "gmail",
        "--mailbox",
        MAILBOX,
        "--db",
        str(path),
    )
    assert out.startswith("REFUSED")
    assert "nothing was sent" in out
    assert err == ""
    # The state machine is untouched by how this is reported: no intent was reserved.
    assert Repository(path).intent(review) is None
    assert "draft_refused" in Repository(path).audit(review)


def test_an_uncertain_attempt_reports_uncertainty_and_a_way_out(tmp_path, monkeypatch, capsys):
    path = tmp_path / "db"
    _, review, _ = approved(path, monkeypatch, capsys)
    repository = Repository(path)
    repository.claim(review)
    repository.finish(review, None)
    out, _ = stopped(monkeypatch, capsys, 3, "draft", review, "--db", str(path))
    assert out.startswith("UNCERTAIN")
    assert f"reconcile {review}" in out
    assert repository.intent(review)[0] == "uncertain"


def test_refused_and_uncertain_never_read_as_the_same_failure(tmp_path, monkeypatch, capsys):
    """Different outcomes, different exit codes, different words, different next moves.

    A script that treated them alike would either retry something that may already exist
    or abandon something that was only refused.
    """
    refused_path, uncertain_path = tmp_path / "refused", tmp_path / "uncertain"
    _, refused_review, _ = approved(refused_path, monkeypatch, capsys, sender=HOSTILE_SENDER)
    monkeypatch.setenv(COMPOSE_TOKEN_VARIABLE, TOKEN)
    refused_out, _ = stopped(
        monkeypatch,
        capsys,
        1,
        "draft",
        refused_review,
        "--provider",
        "gmail",
        "--mailbox",
        MAILBOX,
        "--json",
        "--db",
        str(refused_path),
    )
    _, uncertain_review, _ = approved(uncertain_path, monkeypatch, capsys)
    Repository(uncertain_path).claim(uncertain_review)
    Repository(uncertain_path).finish(uncertain_review, None)
    uncertain_out, _ = stopped(
        monkeypatch, capsys, 3, "draft", uncertain_review, "--json", "--db", str(uncertain_path)
    )
    assert json.loads(refused_out)["outcome"] == "refused"
    assert json.loads(uncertain_out)["outcome"] == "uncertain"
    assert json.loads(refused_out)["state"] is None
    assert json.loads(uncertain_out)["state"] == "uncertain"


def test_a_reconcile_that_finds_nothing_is_still_uncertain(tmp_path, monkeypatch, capsys):
    path = tmp_path / "db"
    _, review, _ = approved(path, monkeypatch, capsys)
    repository = Repository(path)
    repository.claim(review)
    repository.finish(review, None)
    out, _ = stopped(monkeypatch, capsys, 3, "reconcile", review, "--db", str(path))
    assert out.startswith("UNCERTAIN")
    assert repository.intent(review)[0] == "uncertain"


def test_an_approval_the_rules_refuse_is_an_outcome_not_a_usage_error(
    tmp_path, monkeypatch, capsys
):
    """Still the existing bound-authorization machinery; only the reporting is new."""
    path = tmp_path / "db"
    workflow, review, opportunity = ingest(path)
    read = event_of(monkeypatch, capsys, path, opportunity)
    run(
        monkeypatch,
        capsys,
        "status",
        opportunity,
        "--to",
        "rejected",
        "--expect",
        str(read),
        "--actor",
        "operator",
        "--db",
        str(path),
    )
    out, err = stopped(
        monkeypatch, capsys, 1, "approve", review, "--actor", "operator", "--db", str(path)
    )
    assert out.startswith("REFUSED")
    assert "record an active status" in out
    assert err == ""
    assert workflow.repository.authorization(review)["approved"] is None


# --- what the detail view says about action state ---------------------------------------------


def test_the_detail_view_says_where_an_opportunity_stands(tmp_path, monkeypatch, capsys):
    """The operator should not have to open libSQL to know whether they can act."""
    path = tmp_path / "db"
    workflow, review, opportunity = ingest(path)
    printed = run(monkeypatch, capsys, "opportunity", opportunity, "--db", str(path))
    assert "approval   not yet decided" in printed
    assert "draft      not attempted" in printed
    assert "status     new (event 1)" in printed

    run(monkeypatch, capsys, "approve", review, "--actor", "operator", "--db", str(path))
    printed = run(monkeypatch, capsys, "opportunity", opportunity, "--db", str(path))
    assert "approval   approved by operator" in printed
    assert "draft      not attempted" in printed

    run(monkeypatch, capsys, "draft", review, "--db", str(path))
    printed = run(monkeypatch, capsys, "opportunity", opportunity, "--db", str(path))
    assert "draft      created; receipt controlled-" in printed


@pytest.mark.parametrize(
    "settle,expected",
    [
        (lambda repository, review: repository.finish(review, None), "uncertain; reconciliation"),
        (lambda repository, review: None, "attempting; no outcome recorded yet"),
    ],
)
def test_the_detail_view_separates_the_draft_states(
    tmp_path, monkeypatch, capsys, settle, expected
):
    path = tmp_path / "db"
    _, review, opportunity = approved(path, monkeypatch, capsys)
    repository = Repository(path)
    repository.claim(review)
    settle(repository, review)
    printed = run(monkeypatch, capsys, "opportunity", opportunity, "--db", str(path))
    assert expected in printed


def test_a_refusal_shows_as_refused_until_an_attempt_replaces_it(tmp_path, monkeypatch, capsys):
    """Refused is not a lesser uncertain: nothing was attempted, and it says so."""
    path = tmp_path / "db"
    _, review, opportunity = approved(path, monkeypatch, capsys, sender=HOSTILE_SENDER)
    monkeypatch.setenv(COMPOSE_TOKEN_VARIABLE, TOKEN)
    stopped(
        monkeypatch,
        capsys,
        1,
        "draft",
        review,
        "--provider",
        "gmail",
        "--mailbox",
        MAILBOX,
        "--db",
        str(path),
    )
    printed = run(monkeypatch, capsys, "opportunity", opportunity, "--db", str(path))
    assert "draft      refused; nothing was created" in printed
    # An attempt is a fact about an attempt; a refusal describes a draft never proposed.
    Repository(path).claim(review)
    Repository(path).finish(review, "controlled-later")
    printed = run(monkeypatch, capsys, "opportunity", opportunity, "--db", str(path))
    assert "draft      created; receipt controlled-later" in printed


# --- terminal safety and structured fidelity ---------------------------------------------------


def test_an_actor_carrying_an_escape_sequence_is_escaped_for_the_terminal(
    tmp_path, monkeypatch, capsys
):
    """The actor is operator-supplied text and reaches the terminal for the first time
    here, in the approval line and in the outcome the command prints."""
    path = tmp_path / "db"
    _, review, opportunity = ingest(path)
    actor = f"oper{ESC}[2Jator"
    printed = run(monkeypatch, capsys, "approve", review, "--actor", actor, "--db", str(path))
    assert not CONTROL.search(printed.replace("\n", ""))
    assert "\\x1b" in printed
    detail = run(monkeypatch, capsys, "opportunity", opportunity, "--db", str(path))
    assert not CONTROL.search(detail.replace("\n", ""))
    assert "\\x1b" in detail


def test_a_provider_receipt_carrying_an_escape_sequence_is_escaped_too(
    tmp_path, monkeypatch, capsys
):
    """A receipt is whatever the provider returned; it is not this project's text."""
    path = tmp_path / "db"
    _, review, opportunity = approved(path, monkeypatch, capsys)
    repository = Repository(path)
    repository.claim(review)
    repository.finish(review, f"gmail-draft:{ESC}]0;spoofed\x07")
    printed = run(monkeypatch, capsys, "opportunity", opportunity, "--db", str(path))
    assert not CONTROL.search(printed.replace("\n", ""))
    assert "\\x1b" in printed


def test_the_structured_form_keeps_the_value_rather_than_the_escaped_rendering(
    tmp_path, monkeypatch, capsys
):
    """Automation gets what was stored. Escaping is presentation, not data."""
    path = tmp_path / "db"
    _, review, opportunity = ingest(path)
    actor = f"oper{ESC}[2Jator"
    structured = json.loads(
        run(monkeypatch, capsys, "approve", review, "--actor", actor, "--json", "--db", str(path))
    )
    assert ESC in structured["message"]
    assert "\\x1b" not in structured["message"]
    detail = json.loads(
        run(monkeypatch, capsys, "opportunity", opportunity, "--json", "--db", str(path))
    )
    assert detail["action"]["actor"] == actor


def test_a_hostile_status_reason_stays_readable_and_inert(tmp_path, monkeypatch, capsys):
    path = tmp_path / "db"
    _, _, opportunity = ingest(path)
    read = event_of(monkeypatch, capsys, path, opportunity)
    run(
        monkeypatch,
        capsys,
        "status",
        opportunity,
        "--to",
        "interested",
        "--expect",
        str(read),
        "--actor",
        f"oper{ESC}[31mator",
        "--reason",
        f"line one\nline two{ESC}[2J",
        "--db",
        str(path),
    )
    printed = run(monkeypatch, capsys, "opportunity", opportunity, "--db", str(path))
    for line in printed.splitlines():
        assert not CONTROL.search(line), repr(line)
    assert "line one" in printed and "line two" in printed


# --- no shortcut around the machinery ------------------------------------------------------------


def test_the_interface_never_reaches_past_the_workflow(tmp_path):
    """#13 exposes what exists. It does not get its own route to an external write.

    A CLI that called claim(), finish() or a provider directly could create a draft
    without the checks the claim transaction performs -- which is exactly the guarantee
    the previous change was for. This asserts the source, because a test that only
    exercised the happy path would not notice a second route being added beside it.
    """
    source = (pathlib.Path(__file__).parent.parent / "cli.py").read_text(encoding="utf-8")
    for forbidden in (".claim(", ".finish(", ".create(", ".lookup(", ".decide(", "record_status("):
        occurrences = source.count(forbidden)
        if forbidden in (".decide(", "record_status("):
            # The two writes the interface is for, each reached one way only.
            assert occurrences == 1, (forbidden, occurrences)
        else:
            assert occurrences == 0, (forbidden, occurrences)
    assert source.count("workflow.draft(") == 1
    assert source.count("workflow.reconcile(") == 1


def test_a_fault_before_anything_was_reserved_is_not_reported_as_uncertain(
    tmp_path, monkeypatch, capsys
):
    """Uncertain means a provider was contacted and the outcome is unknown.

    A provider that breaks before the claim has contacted nothing, so there is no intent
    to reconcile and reporting one would send the operator looking for a draft that was
    never proposed. A fault is not a state, so it surfaces as a fault.
    """

    def broken(*args, **kwargs):
        raise RuntimeError("provider is misconfigured")

    path = tmp_path / "db"
    _, review, _ = approved(path, monkeypatch, capsys)
    monkeypatch.setattr(ControlledDrafts, "refusal", broken)
    with pytest.raises(RuntimeError, match="misconfigured"):
        run(monkeypatch, capsys, "draft", review, "--db", str(path))
    assert Repository(path).intent(review) is None
