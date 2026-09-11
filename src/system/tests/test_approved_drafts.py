"""End to end: a draft reaches an external mailbox only when the operator authorized it.

This is the first capability where a mistake creates content in somebody's account rather
than a wrong line on a terminal, so the tests start from a hostile recruiter message and
prove what does not happen.
"""

import base64
import json

import pytest

from communications import gmail, gmail_draft
from communications.controlled import ControlledDrafts
from communications.gmail_draft import (
    COMPOSE_TOKEN_VARIABLE,
    GmailComposeCredentials,
    GmailDrafts,
)
from communications.message import Message
from data import store
from data.repository import Repository
from recruiting.models import Profile
from recruiting.ports import DraftRefused
from system import cli
from system.workflow import Workflow

TOKEN = "synthetic-compose-token-value"
MAILBOX = "operator@example.com"
HOSTILE_SENDER = "recruiter@example.com\r\nBcc: attacker@example.com"
JOB_TEXT = (
    "Title: IAM Architect\r\n"
    "Company: Example Corp\r\n"
    "Location: remote\r\n"
    "Skills: Python, SQL\r\n"
    "URL: https://jobs.example.com/roles/1\r\n"
)


def raw_message(sender="recruiter@example.com", subject="A role for you"):
    return (
        f"From: {sender}\r\n"
        f"To: {MAILBOX}\r\n"
        f"Subject: {subject}\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: text/plain; charset="utf-8"\r\n'
        "\r\n"
        f"{JOB_TEXT}"
    ).encode()


def ingest(path, *, sender="recruiter@example.com", subject="A role for you"):
    """Ingest one recruiter message and approve the review it produced."""
    workflow = Workflow(Repository(path), ControlledDrafts(), Profile(("python", "sql")))
    message = Message(
        namespace="gmail:operator@example.com",
        external_id="m1",
        sender=sender,
        subject=subject,
        text=JOB_TEXT,
    )
    review = workflow.intake_message(message)[0]
    return workflow, review


class Recorder:
    """Answers both kinds of Gmail read this suite exercises: identity and reconciliation.

    A profile URL gets the configured identity; anything else -- the drafts listing used by
    lookup() -- gets an empty page, exactly as before this provider had an identity check at
    all. Defaulting identity to MAILBOX means every pre-existing test, which approves and
    then drafts through the same declared mailbox, verifies without anyone here having to
    say so.
    """

    def __init__(self, created=None, identity=MAILBOX):
        self.created = created or {"id": "draft123"}
        self.identity = identity
        self.creates = []
        self.reads = []

    def create(self, url, headers, body):
        self.creates.append((url, body))
        return 200, json.dumps(self.created).encode()

    def read(self, url, headers):
        self.reads.append(url)
        if url.endswith("/profile"):
            return 200, json.dumps({"emailAddress": self.identity}).encode()
        return 200, json.dumps({}).encode()


def approve(workflow, review, *, actor="operator", mailbox=None):
    """Approve for whichever destination `workflow.provider` currently declares.

    A raw `decide()` call now needs a provider identity to authorize anything a Gmail
    provider will later request, and that identity has to match what the provider in the
    test actually is -- not a value chosen independently of it, which is exactly the bug
    this contract exists to close. `mailbox` overrides the namespace the approval binds,
    for tests that deliberately approve for one destination and request another.
    """
    provider = workflow.provider
    namespace = gmail_draft.namespace_for(mailbox) if mailbox else provider.namespace
    workflow.repository.decide(
        review,
        approved=True,
        actor=actor,
        provider=provider.provider,
        provider_namespace=namespace,
    )


def run(monkeypatch, capsys, *arguments):
    monkeypatch.setattr("sys.argv", ["careersignal", *arguments])
    cli.main()
    return capsys.readouterr().out


def refusal(monkeypatch, capsys, *arguments):
    """A command written wrongly: argparse stops it, exit 2, message on stderr."""
    with pytest.raises(SystemExit) as stopped:
        run(monkeypatch, capsys, *arguments)
    assert stopped.value.code == 2, arguments
    return capsys.readouterr().err


def refused(monkeypatch, capsys, *arguments):
    """The system refusing, which is a different thing: exit 1, outcome on stdout."""
    with pytest.raises(SystemExit) as stopped:
        run(monkeypatch, capsys, *arguments)
    assert stopped.value.code == 1, arguments
    return capsys.readouterr().out


# --- the premise the rest of the tests depend on -----------------------------------------


def test_a_hostile_sender_reaches_storage_unchanged(tmp_path):
    """Stated rather than assumed: evidence is never rewritten to make it sendable.

    If normalization silently repaired the From header, every refusal below would be
    testing nothing.
    """
    path = tmp_path / "db"
    workflow, review = ingest(path, sender=HOSTILE_SENDER)
    with store.connection(path) as conn:
        stored = conn.execute("SELECT sender FROM message_sources").fetchone()[0]
    assert "\n" in stored and "attacker@example.com" in stored
    assert workflow.repository.addressing(review)["to"] == stored


# --- refusing an outward write ------------------------------------------------------------


@pytest.mark.parametrize(
    "kind,sender,subject",
    [
        ("recipient", HOSTILE_SENDER, "A role for you"),
        ("subject", "recruiter@example.com", "A role\r\nX-Injected: yes"),
    ],
)
def test_a_hostile_header_is_refused_without_stranding_the_review(tmp_path, kind, sender, subject):
    """A refusal is certain, so it must not be recorded as an unknown external result.

    Recording one would strand the review: an intent locks the decision for
    reconciliation, and reconciliation cannot find a draft that was never created. The
    operator would be left with an approval they cannot use and no way back without
    editing the database by hand.
    """
    path = tmp_path / "db"
    recorder = Recorder()
    workflow, review = ingest(path, sender=sender, subject=subject)
    workflow.provider = GmailDrafts(
        GmailComposeCredentials(TOKEN, MAILBOX), create=recorder.create, read=recorder.read
    )
    approve(workflow, review)
    with pytest.raises(DraftRefused):
        workflow.draft(review)

    assert recorder.creates == [], "a refused draft still contacted Gmail"
    # Nothing was reserved, so there is nothing to unwind and nothing to reconcile.
    assert workflow.repository.intent(review) is None
    assert "draft_refused" in workflow.repository.audit(review)
    assert "draft_uncertain" not in workflow.repository.audit(review)
    # The approval survives the refusal, which is what makes correcting and reevaluating
    # possible at all.
    assert workflow.repository.authorization(review)["approved"] is True
    # Repeating it refuses again rather than degrading into a different state.
    with pytest.raises(DraftRefused):
        workflow.draft(review)
    assert recorder.creates == []


def test_a_corrected_source_can_be_drafted_after_a_refusal(tmp_path):
    """Refuse, correct, reevaluate -- proven end to end rather than described."""
    path = tmp_path / "db"
    recorder = Recorder()
    workflow, refused = ingest(path, sender=HOSTILE_SENDER)
    workflow.provider = GmailDrafts(
        GmailComposeCredentials(TOKEN, MAILBOX), create=recorder.create, read=recorder.read
    )
    approve(workflow, refused)
    with pytest.raises(DraftRefused):
        workflow.draft(refused)

    # The evidence that caused the refusal is retained exactly as it arrived.
    with store.connection(path) as conn:
        stored = conn.execute("SELECT sender FROM message_sources ORDER BY message_id").fetchall()
    assert any("attacker@example.com" in row[0] for row in stored)

    # The operator corrects the source by re-ingesting it, which is this project's only
    # route to a new evaluation; the opportunity dedupes and a fresh review is produced.
    message = Message(
        namespace="gmail:operator@example.com",
        external_id="m2",
        sender="recruiter@example.com",
        subject="A role for you",
        text=JOB_TEXT,
    )
    corrected = workflow.intake_message(message)[0]
    # The job did not change, so replay reuses the review rather than inventing a new one.
    # What changed is where it came from, and the most recent source addresses the draft.
    assert corrected == refused
    assert workflow.repository.addressing(corrected)["to"] == "recruiter@example.com"

    # The old approval does not carry over onto the new target. It was a decision about
    # writing to a particular recipient, and that recipient changed.
    with pytest.raises(ValueError, match="Addressing changed since approval"):
        workflow.draft(corrected)
    assert recorder.creates == []
    assert workflow.repository.intent(corrected) is None

    approve(workflow, corrected)
    receipt = workflow.draft(corrected)
    assert receipt == "gmail-draft:draft123"
    assert len(recorder.creates) == 1
    raw = base64.urlsafe_b64decode(json.loads(recorder.creates[0][1])["message"]["raw"]).decode()
    assert "To: recruiter@example.com" in raw
    assert "attacker@example.com" not in raw


def test_a_source_arriving_after_the_claim_cannot_move_the_target(tmp_path, monkeypatch):
    """The address that was authorized is the address that is written to.

    draft() used to re-read the address after claiming, so a message arriving in that
    interval could redirect an already authorized write. This drives that exact interval:
    a competing source lands between the claim committing and the provider being called.
    """
    path = tmp_path / "db"
    recorder = Recorder()
    workflow, review = ingest(path, sender="jane@example.com")
    workflow.provider = GmailDrafts(
        GmailComposeCredentials(TOKEN, MAILBOX), create=recorder.create, read=recorder.read
    )
    approve(workflow, review)

    original = workflow.repository.claim

    def claim_then_race(review_id, **kwargs):
        claimed = original(review_id, **kwargs)
        # A later message about the same job, from somebody else, lands right here.
        workflow.intake_message(
            Message(
                namespace="gmail:operator@example.com",
                external_id="m2",
                sender="bob@example.com",
                subject="A role for you",
                text=JOB_TEXT,
            )
        )
        return claimed

    monkeypatch.setattr(workflow.repository, "claim", claim_then_race)
    workflow.draft(review)

    assert workflow.repository.addressing(review)["to"] == "bob@example.com"
    raw = base64.urlsafe_b64decode(json.loads(recorder.creates[0][1])["message"]["raw"]).decode()
    assert "To: jane@example.com" in raw, "the write went to a target that was never approved"
    assert "bob@example.com" not in raw


# --- a durable intent settles the outcome before anything else is considered ------------


def later_hostile_source(workflow):
    """A later alert for the same opportunity, carrying an address that would be refused."""
    workflow.intake_message(
        Message(
            namespace="gmail:operator@example.com",
            external_id="m2",
            sender=HOSTILE_SENDER,
            subject="A role for you",
            text=JOB_TEXT,
        )
    )


def refuse_to_preflight(workflow, monkeypatch):
    """Prove the preflight is not merely harmless here, but never reached at all."""

    def unreachable(*args, **kwargs):
        raise AssertionError("a settled intent was re-preflighted against a later source")

    monkeypatch.setattr(workflow.provider, "refusal", unreachable)


def test_a_confirmed_draft_still_replays_after_a_later_hostile_source(tmp_path, monkeypatch):
    """The receipt is a fact about an attempt already made. Later data cannot revise it."""
    path = tmp_path / "db"
    recorder = Recorder()
    workflow, review = ingest(path)
    workflow.provider = GmailDrafts(
        GmailComposeCredentials(TOKEN, MAILBOX), create=recorder.create, read=recorder.read
    )
    approve(workflow, review)
    receipt = workflow.draft(review)
    assert workflow.repository.intent(review) == ("confirmed", receipt)

    later_hostile_source(workflow)
    refuse_to_preflight(workflow, monkeypatch)

    assert workflow.draft(review) == receipt
    assert len(recorder.creates) == 1, "replay wrote a second draft"
    assert workflow.repository.intent(review) == ("confirmed", receipt)


def test_an_uncertain_draft_stays_reconcilable_after_a_later_hostile_source(tmp_path, monkeypatch):
    """Uncertain means an external write may have happened. That does not become a refusal."""
    path = tmp_path / "db"
    workflow, review = ingest(path)
    reader = Recorder().read

    def explode(url, headers, body):
        raise OSError("connection reset after the request was sent")

    workflow.provider = GmailDrafts(
        GmailComposeCredentials(TOKEN, MAILBOX), create=explode, read=reader
    )
    approve(workflow, review)
    with pytest.raises(OSError):
        workflow.draft(review)
    assert workflow.repository.intent(review)[0] == "uncertain"

    later_hostile_source(workflow)
    refuse_to_preflight(workflow, monkeypatch)

    # Still not retried, and still not reclassified as a refusal.
    assert workflow.draft(review) is None
    assert workflow.repository.intent(review)[0] == "uncertain"
    assert "draft_refused" not in workflow.repository.audit(review)
    # Reconciliation remains the only route, and it is unaffected by the later source.
    assert workflow.reconcile(review) is None
    assert workflow.repository.intent(review)[0] == "uncertain"


@pytest.mark.parametrize(
    "receipt,expected", [("gmail-draft:from-elsewhere", "gmail-draft:from-elsewhere"), (None, None)]
)
def test_an_intent_settled_during_the_preflight_still_wins(tmp_path, receipt, expected):
    """The refusal path never reaches claim(), so its guard has to live in refuse().

    Interleaving: this caller reads intent() and finds none; another settles one; a later
    hostile source arrives; this caller's preflight returns a reason. Without an atomic
    check at the refusal, recording it raised ValueError and the caller lost the receipt --
    later data revising an external attempt that had already become authoritative.
    """
    path = tmp_path / "db"
    workflow, review = ingest(path, sender="jane@example.com")
    workflow.repository.decide(review, approved=True, actor="operator")

    class Racing:
        """Settles the intent, then lands hostile data, then refuses -- inside the preflight."""

        provider = "controlled"
        namespace = "controlled"

        def refusal(self, key, body, *, to="", subject_line=""):
            workflow.repository.claim(review)
            workflow.repository.finish(review, receipt)
            later_hostile_source(workflow)
            return "Recipient contains prohibited control characters"

        def create(self, *args, **kwargs):
            raise AssertionError("a settled intent still reached the provider")

    workflow.provider = Racing()
    assert workflow.repository.intent(review) is None, "the race has not started yet"
    assert workflow.draft(review) == expected

    # The external state that became authoritative first is exactly what it was left as.
    assert workflow.repository.intent(review) == (
        "confirmed" if receipt else "uncertain",
        receipt,
    )
    # And no refusal was recorded against an attempt that had already happened.
    assert "draft_refused" not in workflow.repository.audit(review)
    assert workflow.repository.audit(review).count("draft_intent") == 1


def test_a_failure_after_the_provider_was_contacted_stays_uncertain(tmp_path):
    """The opposite case, unchanged: past the request the outcome genuinely is unknown."""
    path = tmp_path / "db"
    workflow, review = ingest(path)

    def explode(url, headers, body):
        raise OSError("connection reset after the request was sent")

    workflow.provider = GmailDrafts(
        GmailComposeCredentials(TOKEN, MAILBOX), create=explode, read=Recorder().read
    )
    approve(workflow, review)
    with pytest.raises(OSError):
        workflow.draft(review)
    assert workflow.repository.intent(review)[0] == "uncertain"
    assert "draft_uncertain" in workflow.repository.audit(review)
    # And it is never blindly retried.
    assert workflow.draft(review) is None


def test_an_unapproved_review_never_reaches_the_provider(tmp_path):
    path = tmp_path / "db"
    recorder = Recorder()
    workflow, review = ingest(path)
    workflow.provider = GmailDrafts(
        GmailComposeCredentials(TOKEN, MAILBOX), create=recorder.create, read=recorder.read
    )
    with pytest.raises(ValueError, match="Explicit approval is required"):
        workflow.draft(review)
    assert recorder.creates == []


def test_an_ended_opportunity_never_reaches_the_provider(tmp_path):
    path = tmp_path / "db"
    recorder = Recorder()
    workflow, review = ingest(path)
    workflow.provider = GmailDrafts(
        GmailComposeCredentials(TOKEN, MAILBOX), create=recorder.create, read=recorder.read
    )
    approve(workflow, review)
    with store.connection(path) as conn:
        opportunity = conn.execute(
            "SELECT opportunity_id FROM reviews WHERE id=?", (review,)
        ).fetchone()[0]
    workflow.repository.record_status(opportunity, "rejected", actor="operator", reason="passed")
    with pytest.raises(ValueError, match="status is rejected"):
        workflow.draft(review)
    assert recorder.creates == []


# --- the authorized path ------------------------------------------------------------------


def test_an_approved_draft_is_created_once_and_addressed_to_the_recruiter(tmp_path):
    path = tmp_path / "db"
    recorder = Recorder()
    workflow, review = ingest(path)
    workflow.provider = GmailDrafts(
        GmailComposeCredentials(TOKEN, MAILBOX), create=recorder.create, read=recorder.read
    )
    approve(workflow, review)
    receipt = workflow.draft(review)
    assert receipt == "gmail-draft:draft123"
    assert len(recorder.creates) == 1
    raw = base64.urlsafe_b64decode(json.loads(recorder.creates[0][1])["message"]["raw"]).decode()
    assert "To: recruiter@example.com" in raw
    assert "Bcc" not in raw
    # Replay does not write again: the confirmed receipt is returned from the record.
    assert workflow.draft(review) == receipt
    assert len(recorder.creates) == 1


# --- an approval and an intent bind the destination too -----------------------------------


def test_a_controlled_intent_refuses_a_later_gmail_request(tmp_path):
    """One review, one intent. A different provider does not get a second attempt."""
    workflow, review = ingest(tmp_path / "db")
    approve(workflow, review)
    receipt = workflow.draft(review)
    assert receipt.startswith("controlled-")

    recorder = Recorder()
    workflow.provider = GmailDrafts(
        GmailComposeCredentials(TOKEN, MAILBOX), create=recorder.create, read=recorder.read
    )
    with pytest.raises(ValueError, match="does not match the draft intent"):
        workflow.draft(review)

    assert recorder.creates == [] and recorder.reads == [], "a mismatch contacted Gmail"
    assert workflow.repository.intent(review) == ("confirmed", receipt)


def test_a_gmail_approval_refuses_a_request_for_a_different_mailbox(tmp_path):
    """The declared mismatch is free: it is refused before anything is contacted."""
    workflow, review = ingest(tmp_path / "db")
    workflow.provider = GmailDrafts(GmailComposeCredentials(TOKEN, MAILBOX))
    approve(workflow, review)

    recorder = Recorder()
    workflow.provider = GmailDrafts(
        GmailComposeCredentials(TOKEN, "bob@example.com"),
        create=recorder.create,
        read=recorder.read,
    )
    with pytest.raises(ValueError, match="does not match the approval"):
        workflow.draft(review)

    assert recorder.reads == [], "the mismatch was declared and needed no profile read"
    assert recorder.creates == []
    assert workflow.repository.intent(review) is None


def test_a_verified_mailbox_mismatch_is_refused_after_the_profile_read(tmp_path):
    """The declared mailbox matched; the credential behind it did not.

    A profile read is allowed here -- it is how the mismatch is discovered -- but no draft
    is posted and nothing is reserved once it is.
    """
    workflow, review = ingest(tmp_path / "db")
    workflow.provider = GmailDrafts(GmailComposeCredentials(TOKEN, MAILBOX))
    approve(workflow, review)

    recorder = Recorder(identity="someone-else@example.com")
    workflow.provider = GmailDrafts(
        GmailComposeCredentials(TOKEN, MAILBOX), create=recorder.create, read=recorder.read
    )
    with pytest.raises(ValueError, match="Verified provider identity does not match"):
        workflow.draft(review)

    assert any(url.endswith("/profile") for url in recorder.reads), "identity was never checked"
    assert recorder.creates == [], "a verified mismatch still posted a draft"
    assert workflow.repository.intent(review) is None


def test_a_verified_gmail_identity_authorizes_exactly_one_draft(tmp_path):
    """The success path: declared, verified and bound all agree, once."""
    recorder = Recorder()
    workflow, review = ingest(tmp_path / "db")
    workflow.provider = GmailDrafts(
        GmailComposeCredentials(TOKEN, MAILBOX), create=recorder.create, read=recorder.read
    )
    approve(workflow, review)
    receipt = workflow.draft(review)

    assert receipt == "gmail-draft:draft123"
    assert len(recorder.creates) == 1
    assert workflow.repository.intent_identity(review) == {
        "provider": "gmail",
        "provider_namespace": gmail_draft.namespace_for(MAILBOX),
    }


def test_reconciling_an_uncertain_gmail_intent_with_the_wrong_mailbox_is_refused(tmp_path):
    """An uncertain attempt belongs to one destination and cannot be read for another."""
    workflow, review = ingest(tmp_path / "db")

    def explode(url, headers, body):
        raise OSError("connection reset after the request was sent")

    workflow.provider = GmailDrafts(
        GmailComposeCredentials(TOKEN, MAILBOX), create=explode, read=Recorder().read
    )
    approve(workflow, review)
    with pytest.raises(OSError):
        workflow.draft(review)
    assert workflow.repository.intent(review)[0] == "uncertain"

    recorder = Recorder()
    workflow.provider = GmailDrafts(
        GmailComposeCredentials(TOKEN, "bob@example.com"),
        create=recorder.create,
        read=recorder.read,
    )
    with pytest.raises(ValueError, match="does not match the draft intent"):
        workflow.reconcile(review)
    assert recorder.reads == [], "reconciliation looked up drafts in the wrong mailbox"


def test_reconciling_an_uncertain_gmail_intent_with_the_verified_mailbox_proceeds(tmp_path):
    """The matching destination is unaffected: reconciliation proceeds as before."""
    workflow, review = ingest(tmp_path / "db")

    def explode(url, headers, body):
        raise OSError("connection reset after the request was sent")

    workflow.provider = GmailDrafts(
        GmailComposeCredentials(TOKEN, MAILBOX), create=explode, read=Recorder().read
    )
    approve(workflow, review)
    with pytest.raises(OSError):
        workflow.draft(review)

    recorder = Recorder()
    workflow.provider = GmailDrafts(
        GmailComposeCredentials(TOKEN, MAILBOX), create=recorder.create, read=recorder.read
    )
    assert workflow.reconcile(review) is None
    assert workflow.repository.intent(review)[0] == "uncertain"


def test_reconciling_refuses_a_credential_that_verifies_as_someone_else(tmp_path, monkeypatch):
    """The declared mailbox matched the intent; the credential behind it did not.

    A declared match is not proof: Gmail's own lookup does not care what --mailbox was
    typed, only what the credential can reach. Without this check, reconciliation would
    silently search a mailbox this attempt was never made against.
    """
    workflow, review = ingest(tmp_path / "db")

    def explode(url, headers, body):
        raise OSError("connection reset after the request was sent")

    workflow.provider = GmailDrafts(
        GmailComposeCredentials(TOKEN, MAILBOX), create=explode, read=Recorder().read
    )
    approve(workflow, review)
    with pytest.raises(OSError):
        workflow.draft(review)
    assert workflow.repository.intent(review)[0] == "uncertain"

    recorder = Recorder(identity="someone-else@example.com")
    workflow.provider = GmailDrafts(
        GmailComposeCredentials(TOKEN, MAILBOX), create=recorder.create, read=recorder.read
    )

    def unreachable(*args, **kwargs):
        raise AssertionError("looked up drafts with a credential that verified as someone else")

    monkeypatch.setattr(workflow.provider, "lookup", unreachable)
    with pytest.raises(ValueError, match="Verified provider identity does not match"):
        workflow.reconcile(review)
    assert workflow.repository.intent(review)[0] == "uncertain"


# --- the read grant is not a write grant ---------------------------------------------------


def test_the_read_adapter_still_refuses_every_scope_but_readonly():
    """PR #7's guarantee is not weakened by this one; the two credentials stay separate."""
    with pytest.raises(ValueError):
        gmail.GmailCredentials(TOKEN, MAILBOX, scopes=(gmail_draft.COMPOSE_SCOPE,))
    with pytest.raises(ValueError):
        gmail_draft.GmailComposeCredentials(TOKEN, MAILBOX, scopes=(gmail.READONLY_SCOPE,))
    assert gmail.TOKEN_VARIABLE != COMPOSE_TOKEN_VARIABLE


def test_the_reader_cannot_reach_the_drafts_collection():
    with pytest.raises(gmail.GmailError):
        gmail.readable_url(gmail.API_ROOT + "users/me/drafts")


# --- the CLI ------------------------------------------------------------------------------


def test_the_external_provider_is_never_the_default(tmp_path, monkeypatch, capsys):
    """Creating content in a mailbox has to be asked for, not fallen into."""
    path = tmp_path / "db"
    _, review = ingest(path)

    def refuse(*args, **kwargs):
        raise AssertionError("the controlled path constructed a Gmail draft writer")

    monkeypatch.setattr(gmail_draft.GmailDrafts, "__init__", refuse)
    monkeypatch.setenv(COMPOSE_TOKEN_VARIABLE, TOKEN)
    run(monkeypatch, capsys, "approve", review, "--actor", "operator", "--db", str(path))
    printed = json.loads(run(monkeypatch, capsys, "draft", review, "--json", "--db", str(path)))
    assert printed["state"] == "confirmed"
    assert printed["outcome"] == "accepted"
    assert printed["receipt"].startswith("controlled-")
    # The same run read by a person rather than a script: replaying the settled intent
    # reports the state it is in, not a second creation.
    replayed = run(monkeypatch, capsys, "draft", review, "--db", str(path))
    assert replayed.startswith("ACCEPTED")
    assert printed["receipt"] in replayed


def test_the_gmail_provider_requires_its_own_token_and_a_mailbox(tmp_path, monkeypatch, capsys):
    path = tmp_path / "db"
    _, review = ingest(path)
    monkeypatch.delenv(COMPOSE_TOKEN_VARIABLE, raising=False)
    assert COMPOSE_TOKEN_VARIABLE in refusal(
        monkeypatch, capsys, "draft", review, "--provider", "gmail", "--db", str(path)
    )
    monkeypatch.setenv(COMPOSE_TOKEN_VARIABLE, TOKEN)
    assert "--mailbox" in refusal(
        monkeypatch, capsys, "draft", review, "--provider", "gmail", "--db", str(path)
    )
    # The read token is not accepted in place of the compose token.
    monkeypatch.delenv(COMPOSE_TOKEN_VARIABLE, raising=False)
    monkeypatch.setenv(gmail.TOKEN_VARIABLE, TOKEN)
    assert COMPOSE_TOKEN_VARIABLE in refusal(
        monkeypatch,
        capsys,
        "draft",
        review,
        "--provider",
        "gmail",
        "--mailbox",
        MAILBOX,
        "--db",
        str(path),
    )


def test_approving_requires_an_actor_and_a_review(tmp_path, monkeypatch, capsys):
    path = tmp_path / "db"
    _, review = ingest(path)
    assert "--actor" in refusal(monkeypatch, capsys, "approve", review, "--db", str(path))
    assert "review id" in refusal(
        monkeypatch, capsys, "approve", "--actor", "operator", "--db", str(path)
    )
    assert "review id" in refusal(monkeypatch, capsys, "draft", "--db", str(path))
    assert "--actor" in refusal(
        monkeypatch, capsys, "approve", review, "--actor", "   ", "--db", str(path)
    )


def test_a_changed_review_is_refused_at_the_command_line_with_a_reason(
    tmp_path, monkeypatch, capsys
):
    path = tmp_path / "db"
    _, review = ingest(path)
    run(monkeypatch, capsys, "approve", review, "--actor", "operator", "--db", str(path))
    with store.connection(path) as conn, store.transaction(conn):
        conn.execute("UPDATE reviews SET draft='different wording' WHERE id=?", (review,))
    assert "approve it again" in refused(monkeypatch, capsys, "draft", review, "--db", str(path))
