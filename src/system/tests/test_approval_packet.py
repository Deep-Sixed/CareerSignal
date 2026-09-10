"""An operator cannot approve an outward draft they have not been shown.

The approval binds a recipient, a subject and an exact wording, and the claim re-verifies
all three before anything leaves this machine. Until now none of them appeared in the
human-readable view: the operator approved a binding to values they could not see. These
tests hold the view and the authorization to the same material.

    what the operator sees == what the approval binds == what the claim verifies
"""

import json
import re

import pytest

from communications import gmail, gmail_draft
from communications.controlled import ControlledDrafts
from communications.message import Message
from data import store
from data.repository import Repository
from recruiting.models import Profile, fingerprint
from system import cli
from system.workflow import Workflow

ESC = chr(0x1B)
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
SENDER = "jane.recruiter@example.com"
SUBJECT = "A role for you"
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


def ingest(path, *, external_id="m1", sender=SENDER, subject=SUBJECT):
    workflow = Workflow(Repository(path), ControlledDrafts(), Profile(("python", "sql")))
    review = workflow.intake_message(
        Message(
            namespace="gmail:operator@example.com",
            external_id=external_id,
            sender=sender,
            subject=subject,
            text=JOB_TEXT,
        )
    )[0]
    return workflow, review, workflow.repository.opportunities()[0]["id"]


def run(monkeypatch, capsys, *arguments):
    monkeypatch.setattr("sys.argv", ["careersignal", *arguments])
    cli.main()
    return capsys.readouterr().out


def stopped(monkeypatch, capsys, code, *arguments):
    with pytest.raises(SystemExit) as exit_code:
        run(monkeypatch, capsys, *arguments)
    assert exit_code.value.code == code, (arguments, exit_code.value.code)
    printed = capsys.readouterr()
    return printed.out, printed.err


def shown(monkeypatch, capsys, path, opportunity):
    return json.loads(
        run(monkeypatch, capsys, "opportunity", opportunity, "--json", "--db", str(path))
    )["bound"]


def snapshot(path):
    with store.connection(path) as conn:
        return {t: conn.execute(f"SELECT * FROM {t}").fetchall() for t in TABLES}


# --- the invariant -----------------------------------------------------------------------


def test_what_the_operator_sees_is_what_the_approval_binds(tmp_path, monkeypatch, capsys):
    """The packet on screen and the digests in the decision are the same material."""
    path = tmp_path / "db"
    _, review, opportunity = ingest(path)
    packet = shown(monkeypatch, capsys, path, opportunity)
    assert packet["to"] == SENDER
    assert packet["subject"] == SUBJECT
    assert packet["review"] == review
    assert packet["source"]

    run(monkeypatch, capsys, "approve", review, "--actor", "operator", "--db", str(path))
    authorization = Repository(path).authorization(review)
    # Not "the values look right" but "the approval is bound to exactly these".
    assert authorization["bound_addressing"] == fingerprint([packet["to"], packet["subject"]])
    assert authorization["bound_draft"] == fingerprint(packet["wording"])
    assert authorization["bound_source"] == packet["source"]


def test_the_human_view_shows_the_same_packet_as_the_structured_one(tmp_path, monkeypatch, capsys):
    path = tmp_path / "db"
    _, _, opportunity = ingest(path)
    packet = shown(monkeypatch, capsys, path, opportunity)
    printed = run(monkeypatch, capsys, "opportunity", opportunity, "--db", str(path))
    assert f"recipient  {packet['to']}" in printed
    assert f"subject    {packet['subject']}" in printed
    assert f"review     {packet['review']}" in printed
    assert f"source     {packet['source']}" in printed
    assert packet["wording"] in printed


def test_the_view_reads_the_bound_wording_rather_than_the_payload_copy(
    tmp_path, monkeypatch, capsys
):
    """The reviews row holds the wording twice: the column an approval's digest is taken
    over, and a copy inside the JSON payload. They are written from the same value, so a
    view reading either one looks correct. Only the column is what gets bound, so the
    view has to read that one -- otherwise the agreement is a coincidence of two writes
    rather than a property of the code."""
    path = tmp_path / "db"
    _, review, opportunity = ingest(path)
    with store.connection(path) as conn, store.transaction(conn):
        conn.execute("UPDATE reviews SET draft='the bound wording' WHERE id=?", (review,))
    printed = run(monkeypatch, capsys, "opportunity", opportunity, "--db", str(path))
    assert "the bound wording" in printed
    assert shown(monkeypatch, capsys, path, opportunity)["wording"] == "the bound wording"
    # The payload copy still says what it always said, and is not what was shown.
    payload = json.loads(
        run(monkeypatch, capsys, "opportunity", opportunity, "--json", "--db", str(path))
    )["packet"]
    assert payload["draft"] != "the bound wording"
    assert payload["draft"] not in printed


def test_an_opportunity_with_no_current_review_binds_nothing(tmp_path, monkeypatch, capsys):
    path = tmp_path / "db"
    _, _, opportunity = ingest(path)
    with store.connection(path) as conn, store.transaction(conn):
        conn.execute("UPDATE opportunities SET current_review=NULL WHERE id=?", (opportunity,))
    printed = run(monkeypatch, capsys, "opportunity", opportunity, "--db", str(path))
    assert "no current review" in printed
    assert "recipient" not in printed
    assert shown(monkeypatch, capsys, path, opportunity) is None


# --- a moved target is visible, and the old approval no longer authorizes it ----------------


def test_a_corrected_source_changes_the_packet_and_the_old_approval_stops_authorizing(
    tmp_path, monkeypatch, capsys
):
    """The second half is #12's behaviour, demonstrated here rather than reimplemented:
    the point is that the operator can now see the target move before the refusal."""
    path = tmp_path / "db"
    workflow, review, opportunity = ingest(path)
    before = shown(monkeypatch, capsys, path, opportunity)
    run(monkeypatch, capsys, "approve", review, "--actor", "operator", "--db", str(path))

    workflow.intake_message(
        Message(
            namespace="gmail:operator@example.com",
            external_id="m2",
            sender="bob.other@example.com",
            subject="A different subject",
            text=JOB_TEXT,
        )
    )
    after = shown(monkeypatch, capsys, path, opportunity)
    assert after["to"] == "bob.other@example.com"
    assert after["subject"] == "A different subject"
    assert after["review"] == before["review"], "the review did not change; its addressing did"
    assert "recipient  bob.other@example.com" in run(
        monkeypatch, capsys, "opportunity", opportunity, "--db", str(path)
    )

    out, _ = stopped(monkeypatch, capsys, 1, "draft", review, "--db", str(path))
    assert out.startswith("REFUSED")
    assert "Addressing changed since approval" in out
    assert Repository(path).intent(review) is None


# --- the view only reads --------------------------------------------------------------------


def test_showing_the_packet_writes_nothing_and_builds_no_provider(tmp_path, monkeypatch, capsys):
    path = tmp_path / "db"
    _, review, opportunity = ingest(path)
    run(monkeypatch, capsys, "approve", review, "--actor", "operator", "--db", str(path))
    before = snapshot(path)

    def refuse(*args, **kwargs):
        raise AssertionError("showing the approval packet built a client")

    monkeypatch.setattr(gmail_draft.GmailDrafts, "__init__", refuse)
    monkeypatch.setattr(gmail.GmailReader, "__init__", refuse)
    monkeypatch.setattr(ControlledDrafts, "__init__", refuse)
    for extra in ((), ("--json",)):
        run(monkeypatch, capsys, "opportunity", opportunity, *extra, "--db", str(path))
    assert snapshot(path) == before


# --- outside text reaching a terminal ---------------------------------------------------------


HOSTILE_SENDER = f"recruiter@example.com{ESC}]0;spoofed\x07"
HOSTILE_SUBJECT = f"A role{ESC}[2J for you"


def test_a_hostile_recipient_and_subject_are_escaped_for_the_terminal(
    tmp_path, monkeypatch, capsys
):
    """These are the first recruiter-supplied header values the view prints."""
    path = tmp_path / "db"
    _, _, opportunity = ingest(path, sender=HOSTILE_SENDER, subject=HOSTILE_SUBJECT)
    printed = run(monkeypatch, capsys, "opportunity", opportunity, "--db", str(path))
    for line in printed.splitlines():
        assert not CONTROL.search(line), repr(line)
    assert "\\x1b" in printed


def test_the_structured_packet_keeps_the_value_that_was_stored(tmp_path, monkeypatch, capsys):
    """Automation must see what the approval will bind, not a rendering of it."""
    path = tmp_path / "db"
    _, _, opportunity = ingest(path, sender=HOSTILE_SENDER, subject=HOSTILE_SUBJECT)
    packet = shown(monkeypatch, capsys, path, opportunity)
    assert packet["to"] == HOSTILE_SENDER
    assert packet["subject"] == HOSTILE_SUBJECT
    assert "\\x1b" not in packet["to"]
    # And the stored evidence is untouched by having been displayed.
    with store.connection(path) as conn:
        stored = conn.execute("SELECT sender,subject FROM message_sources").fetchone()
    assert stored == (HOSTILE_SENDER, HOSTILE_SUBJECT)
