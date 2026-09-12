"""Gmail intake against recorded synthetic payloads. No mailbox is contacted."""

import base64
import json

import pytest

from communications.controlled import ControlledDrafts
from communications.gmail import GmailCredentials, GmailReader
from data import store
from data.repository import Repository
from recruiting.models import Profile
from system.workflow import Workflow

MAILBOX = "operator@example.com"
JOB = (
    "Title: Application Engineer\r\n"
    "Company: Example Company\r\n"
    "Location: remote\r\n"
    "Skills: Python, SQL\r\n"
    "URL: https://jobs.example.com/roles/1\r\n"
)


def raw(subject="Job alert", header_id="<alert@example.com>", body=JOB):
    return (
        f"From: alerts@example.com\r\nTo: {MAILBOX}\r\nSubject: {subject}\r\n"
        f"Message-ID: {header_id}\r\nMIME-Version: 1.0\r\n"
        'Content-Type: text/plain; charset="utf-8"\r\n\r\n'
        f"{body}"
    ).encode()


class Mailbox:
    """A recorded Gmail response set. It answers reads and nothing else."""

    def __init__(self, messages, on_request=None):
        self.messages = dict(messages)
        self.on_request = on_request

    def __call__(self, url, headers):
        if self.on_request:
            self.on_request()
        if url.endswith("/profile"):
            return 200, json.dumps({"emailAddress": MAILBOX}).encode()
        if "/messages/" in url:
            identifier = url.split("/messages/", 1)[1].split("?", 1)[0]
            body = self.messages[identifier]
            return 200, json.dumps(
                {"id": identifier, "raw": base64.urlsafe_b64encode(body).decode().rstrip("=")}
            ).encode()
        return 200, json.dumps({"messages": [{"id": i} for i in self.messages]}).encode()


def reader(messages, on_request=None):
    return GmailReader(GmailCredentials("synthetic-token", MAILBOX), Mailbox(messages, on_request))


def workflow(path):
    return Workflow(Repository(path), ControlledDrafts(), Profile(("python", "sql")))


def sources(path, message_key):
    with store.connection(path) as conn:
        return conn.execute(
            "SELECT namespace,external_id,sender,subject,format,parser_version "
            "FROM message_sources WHERE message_id=?",
            (message_key,),
        ).fetchone()


def test_a_read_mailbox_produces_reviews_with_provider_provenance(tmp_path):
    path = tmp_path / "db"
    flow = workflow(path)
    message = reader({"1111aaaa2222": raw()}).messages()[0]
    reviews = flow.intake_message(message)
    assert len(reviews) == 1
    assert sources(path, message.key) == (
        "gmail:operator@example.com",
        "1111aaaa2222",
        "alerts@example.com",
        "Job alert",
        "text",
        "labeled-v1",
    )


def test_reading_the_same_mailbox_twice_creates_nothing_new(tmp_path):
    path = tmp_path / "db"
    flow = workflow(path)
    mailbox = {"1111aaaa2222": raw()}
    first = flow.intake_message(reader(mailbox).messages()[0])
    # A separate read, a separate reader, a separate parse: only the provider identity is
    # shared, which is the whole basis of the replay contract.
    second = flow.intake_message(reader(mailbox).messages()[0])
    assert first == second and len(first) == 1
    with store.connection(path) as conn:
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM opportunities").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM reviews").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM draft_intents").fetchone()[0] == 0


def test_intake_never_creates_a_draft(tmp_path):
    """Reading a mailbox must not be a step towards contacting anyone."""
    flow = workflow(tmp_path / "db")
    review = flow.intake_message(reader({"1111aaaa2222": raw()}).messages()[0])[0]
    assert flow.repository.intent(review) is None
    assert flow.provider.calls == 0
    with pytest.raises(ValueError, match="approval"):
        flow.draft(review)
    assert flow.repository.audit(review) == ["review_created"]


def test_a_provider_identifier_reused_with_changed_content_is_refused(tmp_path):
    """Gmail identifiers are immutable; the same one carrying new content is not a replay."""
    flow = workflow(tmp_path / "db")
    flow.intake_message(reader({"1111aaaa2222": raw()}).messages()[0])
    changed = reader({"1111aaaa2222": raw(subject="Different alert")}).messages()[0]
    with pytest.raises(ValueError, match="reused with different content"):
        flow.intake_message(changed)


def test_two_messages_sharing_an_rfc822_header_stay_separate(tmp_path):
    """The sender chooses the Message-ID header; it must not be able to merge two messages."""
    path = tmp_path / "db"
    flow = workflow(path)
    mailbox = {
        "aaa1": raw(header_id="<same@example.com>"),
        "bbb2": raw(header_id="<same@example.com>", subject="Second alert"),
    }
    keys = {m.key for m in reader(mailbox).messages()}
    assert len(keys) == 2
    for message in reader(mailbox).messages():
        flow.intake_message(message)
    with store.connection(path) as conn:
        assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 2
        # One job URL, so one opportunity: dedupe is by job, not by message.
        assert conn.execute("SELECT count(*) FROM opportunities").fetchone()[0] == 1


def test_no_mailbox_read_happens_while_a_write_transaction_is_held(tmp_path):
    """The contract forbids holding a write reservation open across a network call."""
    path = tmp_path / "db"
    flow = workflow(path)
    observed = []

    def check():
        with store.connection(path) as conn:
            try:
                with store.transaction(conn, timeout=0.05):
                    observed.append("writable")
            except TimeoutError:
                observed.append("blocked")

    messages = reader({"aaa1": raw(), "bbb2": raw(subject="Second")}, on_request=check).messages()
    for message in messages:
        flow.intake_message(message)
    assert observed and set(observed) == {"writable"}
