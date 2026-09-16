"""Taking material in from the browser, tested by what cannot cross the boundary.

Intake is the first thing this surface does that lets *outside* content in. Everything the
web layer did before this read what CareerSignal already held, or acted on a decision an
operator had already made; these two commands accept bytes a recruiter wrote and a mailbox a
credential can reach. So the questions here are narrower than "does it import":

  - does the material go through the one pipeline, or has a second one appeared;
  - can the browser, or the material, choose the profile it is judged against;
  - can the browser choose a mailbox, a credential, or a more powerful Gmail operation;
  - does a Gmail read that fails partway leave anything behind;
  - is a repeat import the storage's idempotency, or a new invention.

The Gmail adapter is exercised through its injected transport, so no test here contacts a
mailbox and CI never depends on a live credential.
"""

import base64
import hashlib
import http.client
import json
import socket
import threading

import pytest

from communications.gmail import GmailCredentials, GmailError, GmailReader
from communications.gmail_draft import COMPOSE_TOKEN_VARIABLE
from communications.message import MAX_MESSAGE_BYTES, Message
from data import store
from data.repository import Repository
from recruiting.models import Profile
from system.intake import IntakeActions
from system.web import server as web

MAILBOX = "operator@example.com"
NAMESPACE = "gmail:" + MAILBOX
JOB = (
    "Title: IAM Architect\r\n"
    "Company: Example Corp\r\n"
    "Location: remote\r\n"
    "Skills: Python, SQL\r\n"
    "URL: https://jobs.example.com/roles/1\r\n"
)
SECOND_JOB = (
    "Title: Access Engineer\r\n"
    "Company: Example Corp\r\n"
    "Location: remote\r\n"
    "Skills: Python\r\n"
    "URL: https://jobs.example.com/roles/2\r\n"
)
NOTHING = "Thanks for connecting. Let us know if you would like to hear about roles.\r\n"
SKILLS = ("python", "sql")


def eml(body=JOB, subject="A role for you", header_id="<alert@example.com>") -> bytes:
    return (
        f"From: recruiter@example.com\r\nTo: {MAILBOX}\r\nSubject: {subject}\r\n"
        f"Message-ID: {header_id}\r\nMIME-Version: 1.0\r\n"
        'Content-Type: text/plain; charset="utf-8"\r\n\r\n'
        f"{body}"
    ).encode()


class Mailbox:
    """A recorded Gmail response set, and a record of every URL it was asked for.

    It answers reads and nothing else, exactly as the live adapter can only make reads. What
    each test does with `self.asked` is assert that a browser request became the reads it was
    supposed to become -- the right query, the right labels, the right bound -- and no others.
    """

    def __init__(self, messages, *, identity=MAILBOX, unreadable=(), fail=None):
        self.messages = dict(messages)
        self.identity = identity
        self.unreadable = set(unreadable)
        self.fail = fail
        self.asked = []

    def __call__(self, url, headers):
        self.asked.append(url)
        if self.fail is not None and "/profile" not in url:
            return self.fail, b'{"error": {"message": "nope"}}'
        if url.endswith("/profile"):
            return 200, json.dumps({"emailAddress": self.identity}).encode()
        if "/messages/" in url:
            identifier = url.split("/messages/", 1)[1].split("?", 1)[0]
            if identifier in self.unreadable:
                return 500, b'{"error": {"message": "unavailable"}}'
            body = self.messages[identifier]
            return 200, json.dumps(
                {"id": identifier, "raw": base64.urlsafe_b64encode(body).decode().rstrip("=")}
            ).encode()
        return 200, json.dumps({"messages": [{"id": i} for i in self.messages]}).encode()


def reading(mailbox) -> GmailReader:
    return GmailReader(GmailCredentials("synthetic-read-token", MAILBOX), mailbox)


@pytest.fixture
def repository(tmp_path):
    return Repository(tmp_path / "db")


def service(repository, *, skills=SKILLS, locations=None, reader=None) -> IntakeActions:
    return IntakeActions(repository, Profile(tuple(skills), tuple(locations or ["remote"])), reader)


class Client:
    """A raw client for the two intake addresses, so a test varies only what it is about."""

    def __init__(self, surface):
        self.surface = surface

    def send(self, path, **kwargs):
        settings = {
            "method": "POST",
            "origin": True,
            "token": True,
            "content_type": None,
            "length": None,
            "headers": (),
            "body": None,
            "half_close": False,
        } | kwargs
        connection = http.client.HTTPConnection(web.LOOPBACK, self.surface.server_port, timeout=15)
        try:
            connection.putrequest(
                settings["method"], path, skip_host=True, skip_accept_encoding=True
            )
            connection.putheader("Host", self.surface.authority)
            if settings["origin"]:
                connection.putheader(
                    "Origin",
                    self.surface.origin if settings["origin"] is True else settings["origin"],
                )
            if settings["token"]:
                connection.putheader(
                    web.TOKEN_HEADER,
                    self.surface.token if settings["token"] is True else settings["token"],
                )
            if settings["content_type"]:
                connection.putheader("Content-Type", settings["content_type"])
            for name, value in settings["headers"]:
                connection.putheader(name, value)
            body = settings["body"]
            if body is not None and settings["length"] != "omit":
                declared = len(body) if settings["length"] is None else settings["length"]
                connection.putheader("Content-Length", str(declared))
            connection.endheaders(body)
            if settings["half_close"]:
                # What a truncated upload actually looks like: the client stops writing and
                # closes its own send side, so the server sees end-of-file rather than waiting
                # forever for bytes a declaration promised.
                connection.sock.shutdown(socket.SHUT_WR)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    def read(self, path):
        status, payload = self.send(path, method="GET", origin=False, body=None)
        return status, json.loads(payload)

    def eml(self, raw, namespace=NAMESPACE, **kwargs):
        headers = () if namespace is None else ((web.NAMESPACE_HEADER, namespace),)
        settings = {"content_type": web.RFC822, "headers": headers, "body": raw} | kwargs
        status, payload = self.send("/api/v1/intake/eml", **settings)
        return status, json.loads(payload)

    def gmail(self, payload=None, **kwargs):
        settings = {
            "content_type": "application/json",
            "body": json.dumps({} if payload is None else payload).encode("utf-8"),
        } | kwargs
        status, body = self.send("/api/v1/intake/gmail", **settings)
        return status, json.loads(body)


def serving(repository, inbound):
    running = web.Surface(repository, port=0, inbound=inbound)
    thread = threading.Thread(target=running.serve_forever, kwargs={"poll_interval": 0.02})
    thread.daemon = True
    thread.start()
    return running, thread


@pytest.fixture
def launch(repository):
    """One surface per test, with whatever intake authority that test is about."""
    running = []

    def start(inbound):
        surface, thread = serving(repository, inbound)
        running.append((surface, thread))
        return Client(surface)

    yield start
    for surface, thread in running:
        surface.shutdown()
        surface.server_close()
        thread.join(timeout=10)


@pytest.fixture
def client(launch, repository):
    return launch(service(repository))


def stored(repository) -> str:
    """A digest of everything intake could move, read from the ledgers rather than the file.

    Under WAL a write can land entirely in the sidecar and leave the main database file
    byte-identical, so comparing file bytes would agree with a write that happened.
    """
    facts = {
        "opportunities": repository.opportunities(),
        "communications": repository.communications(),
        "timeline": repository.timeline(),
    }
    return hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()


def sources_of(repository, message_key):
    with store.connection(repository.path) as conn:
        return conn.execute(
            "SELECT namespace,external_id,sender,subject,format,parser_version "
            "FROM message_sources WHERE message_id=?",
            (message_key,),
        ).fetchone()


# --- taking in one local message ------------------------------------------------------------


def test_a_local_message_can_be_taken_in_from_the_browser(client, repository):
    status, record = client.eml(eml())
    assert status == 200, record
    assert record["source"] == "eml"
    assert record["reviews"], record
    # The same records the command line would have produced: an opportunity, a communication,
    # and the review the browser was told about.
    rows = repository.opportunities()
    assert [row["company"] for row in rows] == ["Example Corp"]
    assert rows[0]["review"] == record["reviews"][0]
    assert [row["message"] for row in repository.communications()] == [record["message"]]


def test_the_declared_namespace_is_what_reaches_stored_provenance(client, repository):
    """Operator-declared, and stored as declared -- not derived from anything in the message.

    The sender, the subject and the `Message-ID` below all say something different from the
    namespace, and every one of them is written by whoever sent the mail. If provenance came
    from any of them, a recruiter would choose which source CareerSignal believes their
    message arrived in.
    """
    status, record = client.eml(eml(), namespace="archive:2019-exports")
    assert status == 200, record
    assert record["namespace"] == "archive:2019-exports"
    namespace, external_id, sender, subject, _, _ = sources_of(repository, record["message"])
    assert namespace == "archive:2019-exports"
    assert external_id == "<alert@example.com>"
    assert (sender, subject) == ("recruiter@example.com", "A role for you")


@pytest.mark.parametrize(
    "namespace", [None, "", "   ", "\t"], ids=["missing", "empty", "spaces", "tab"]
)
def test_a_message_with_no_declared_source_is_refused(client, repository, namespace):
    """An absent namespace is the operator not having said, not a default to fill in."""
    before = stored(repository)
    status, body = client.eml(eml(), namespace=namespace)
    assert status == 400, body
    assert web.NAMESPACE_HEADER in body["error"]
    assert stored(repository) == before


def test_two_declared_sources_are_refused_rather_than_resolved(client, repository):
    """A request saying two different things about where its material came from.

    Taking the first would be this layer choosing which provenance to believe, and a header
    that can be repeated is a header whose second copy somebody is counting on.
    """
    before = stored(repository)
    status, body = client.eml(
        eml(),
        namespace=None,
        headers=((web.NAMESPACE_HEADER, "one"), (web.NAMESPACE_HEADER, "two")),
    )
    assert status == 400, body
    assert stored(repository) == before


@pytest.mark.parametrize(
    "media",
    ["application/json", "text/plain", "multipart/form-data", "application/octet-stream", ""],
)
def test_the_eml_address_accepts_only_an_rfc822_message(client, repository, media):
    before = stored(repository)
    status, body = client.eml(eml(), content_type=media or None)
    assert status == 415, body
    assert stored(repository) == before


def test_an_oversized_declared_body_is_refused_before_it_is_read(client, repository):
    """On what it declares, not after it has been read into memory.

    The body sent here is tiny; only the declaration is large. A surface that measured after
    reading would have to buffer whatever a caller chose to send first.
    """
    before = stored(repository)
    status, body = client.eml(b"x", length=MAX_MESSAGE_BYTES + 1)
    assert status == 413, body
    assert str(MAX_MESSAGE_BYTES) in body["error"]
    assert stored(repository) == before


def test_the_ceiling_is_the_materials_own_and_not_only_a_header_check(repository):
    """Asserted where the bytes actually are, because the HTTP check never sees them.

    A declared length over the ceiling is refused before the body is read, which is the right
    order and also means the socket test above proves nothing about content. This is the other
    half: a message that really is too large is refused by `Message` itself, so the ceiling
    holds for a caller that lies about its length as well as one that does not.
    """
    oversized = eml(body="x" * (MAX_MESSAGE_BYTES + 1000))
    assert len(oversized) > MAX_MESSAGE_BYTES
    before = stored(repository)
    with pytest.raises(ValueError):
        service(repository).ingest_eml(oversized, NAMESPACE)
    assert stored(repository) == before


def test_a_body_shorter_than_it_declared_is_refused_rather_than_ingested(client, repository):
    """A truncated message parses perfectly well into a message missing most of itself.

    Storing that as evidence would be recording something nobody sent, under a provenance
    that says it arrived whole.
    """
    before = stored(repository)
    raw = eml()
    status, body = client.eml(raw, length=len(raw) + 64, half_close=True)
    assert status == 400, body
    assert stored(repository) == before


@pytest.mark.parametrize(
    "raw",
    [
        b"Content-Type: text/plain; charset=nonsense-9999\r\n\r\nTitle: One\r\n",
        b"MIME-Version: 1.0\r\nContent-Type: multipart/alternative; boundary=b\r\n\r\n"
        b"--b\r\nContent-Type: text/plain\r\n\r\nfirst\r\n"
        b"--b\r\nContent-Type: text/plain\r\n\r\nsecond\r\n--b--\r\n",
        b"Subject: nothing\r\n\r\n",
    ],
    ids=["unusable charset", "two inline bodies", "no body at all"],
)
def test_material_the_parser_refuses_is_a_bad_request_and_writes_nothing(client, repository, raw):
    """`Message` owns every question about the bytes, and answers in its own words."""
    before = stored(repository)
    status, body = client.eml(raw)
    assert status == 400, body
    assert body["error"]
    assert stored(repository) == before


def test_a_message_naming_no_opportunity_is_a_success_with_diagnostics(client, repository):
    """Extraction diagnostics are data. Answering 400 would tell the operator to fix a
    request that was right, and hide the one thing they need to see."""
    status, record = client.eml(eml(body=NOTHING))
    assert status == 200, record
    assert record["reviews"] == []
    assert record["diagnostics"], record
    assert all(row["reason"] for row in record["diagnostics"])
    # It is still a message CareerSignal took in, and the Inbox will show it.
    assert [row["message"] for row in repository.communications()] == [record["message"]]


def test_taking_the_same_message_in_twice_follows_the_storage_it_already_had(client, repository):
    """No second deduplication layer, and no intake id the browser could vary.

    Identity belongs to `Message` and `Repository.ingest`. Pressing the button twice resolves
    to the records that already exist, and says so honestly rather than reporting a new
    message that was not created.
    """
    first_status, first = client.eml(eml())
    second_status, second = client.eml(eml())
    assert (first_status, second_status) == (200, 200)
    assert second["message"] == first["message"]
    assert second["reviews"] == first["reviews"]
    assert second["diagnostics"] == first["diagnostics"]
    assert len(repository.communications()) == 1
    assert len(repository.opportunities()) == 1


def test_the_same_message_under_a_different_declared_source_is_a_different_message(
    client, repository
):
    """Namespace is part of identity, which is the repository's rule and not a new one."""
    _, first = client.eml(eml(), namespace="archive:one")
    _, second = client.eml(eml(), namespace="archive:two")
    assert first["message"] != second["message"]
    assert len(repository.communications()) == 2
    # Two sources, one job: the opportunity is not duplicated by arriving twice.
    assert len(repository.opportunities()) == 1


def test_the_raw_message_never_comes_back_to_the_browser(client):
    """The receipt is a report about what happened, not a second copy of the message.

    The body is recruiter-controlled text on the one origin that holds the launch credential.
    Its labelled excerpts are served by the communications projection an operator can open;
    an intake response echoing the payload would put it somewhere nothing asked for it.
    """
    raw = eml(body="Title: IAM Architect\r\nSECRET-MARKER-IN-BODY\r\n" + JOB)
    status, record = client.eml(raw)
    assert status == 200, record
    printed = json.dumps(record)
    assert "SECRET-MARKER-IN-BODY" not in printed
    assert "Content-Type" not in printed
    assert sorted(record) == [
        "diagnostics",
        "external_id",
        "message",
        "namespace",
        "reviews",
        "source",
    ]


def test_the_eml_address_takes_no_field_at_all_because_it_has_no_place_for_one(client, repository):
    """There is no request shape that could carry a profile.

    The body is the message and the only other input is one header. A caller trying to send
    `{"skills": [...]}` is sending JSON to an address that accepts `message/rfc822`, which is
    refused on the media type before anything looks for fields at all.
    """
    before = stored(repository)
    status, body = client.eml(
        json.dumps({"skills": ["rust"], "locations": ["onsite berlin"]}).encode(),
        content_type="application/json",
    )
    assert status == 415, body
    assert stored(repository) == before


def test_a_query_string_on_the_eml_address_is_refused(client, repository):
    before = stored(repository)
    status, payload = client.send(
        "/api/v1/intake/eml?skills=rust",
        content_type=web.RFC822,
        headers=((web.NAMESPACE_HEADER, NAMESPACE),),
        body=eml(),
    )
    assert status == 400, payload
    assert stored(repository) == before


def test_a_filesystem_path_is_material_rather_than_an_instruction(client, repository, tmp_path):
    """The server resolves no name and opens no file.

    A caller that sends a path has sent a few bytes of text, and those bytes are what the
    parser sees -- so the answer is the parser's refusal, never the contents of that file.
    """
    secret = tmp_path / "private.eml"
    secret.write_bytes(eml(body="Title: Should Never Be Read\r\n" + JOB))
    status, body = client.eml(str(secret).encode())
    assert status == 400, body
    assert "Should Never Be Read" not in json.dumps(body)
    assert repository.communications() == []


# --- taking in a Gmail batch ---------------------------------------------------------------


def test_a_launch_with_no_gmail_credential_contacts_nothing_and_writes_nothing(launch, repository):
    client = launch(service(repository))
    before = stored(repository)
    status, body = client.gmail()
    assert status == 409, body
    assert body["error"] == "intake_unavailable"
    assert stored(repository) == before


def test_a_gmail_import_reads_exactly_the_batch_the_request_bounded(launch, repository):
    mailbox = Mailbox(
        {"aaaa1111": eml(), "bbbb2222": eml(body=SECOND_JOB, header_id="<second@example.com>")}
    )
    client = launch(service(repository, reader=reading(mailbox)))
    status, record = client.gmail({"query": "from:recruiting", "labels": ["INBOX"], "limit": 2})
    assert status == 200, record
    assert record["read"] == 2
    listing = [url for url in mailbox.asked if "/messages?" in url or url.endswith("/messages")]
    assert len(listing) == 1, mailbox.asked
    assert "q=from%3Arecruiting" in listing[0]
    assert "labelIds=INBOX" in listing[0]
    assert "maxResults=2" in listing[0]


def test_the_namespace_reported_is_the_verified_one_and_not_a_supplied_one(launch, repository):
    mailbox = Mailbox({"aaaa1111": eml()})
    client = launch(service(repository, reader=reading(mailbox)))
    status, record = client.gmail()
    assert status == 200, record
    assert record["mailbox"] == NAMESPACE
    namespace, external_id, _, _, _, _ = sources_of(repository, record["messages"][0]["message"])
    # Provenance is Gmail's own identifier, never the RFC822 header the sender wrote.
    assert (namespace, external_id) == (NAMESPACE, "aaaa1111")
    # And the identity was proven before any message was read.
    assert mailbox.asked[0].endswith("/profile")


class Recording:
    """A reader that records what was asked of it, and in which order.

    Deliberately not a `GmailReader`. The real adapter verifies identity inside
    `identifiers()` and `fetch()` as well, so a test using one cannot tell whether the service
    proved the mailbox or merely benefited from the adapter proving it -- and a mutation that
    removed the service's own check passed every test in this file. Redundant guards each need
    their own pinning, or either one alone masks the other's removal.
    """

    def __init__(self, messages=(), *, declared=NAMESPACE, proven=None):
        self.namespace = declared
        self._messages = tuple(messages)
        self._proven = declared if proven is None else proven
        self.calls = []

    def verify_identity(self):
        self.calls.append("verify")
        if self._proven != self.namespace:
            raise GmailError(f"token belongs to {self._proven}, not {self.namespace}")
        return self._proven

    def messages(self, *, query="", label_ids=(), limit=25):
        self.calls.append(("messages", query, label_ids, limit))
        return self._messages


def test_the_service_proves_the_mailbox_itself_before_it_reads_anything(launch, repository):
    """The ordering asserted against a reader that does not prove it on the service's behalf.

    `verify identity, then read, then write` is the intake service's contract. The adapter
    happens to check again on every request it makes, which is worth having and is not the
    same guarantee: a reader swapped for one that checks only when asked would silently read
    an unproven mailbox, and this is what says the service asks.
    """
    reader = Recording([Message.from_bytes(eml(), namespace=NAMESPACE, provider_id="aaaa1111")])
    client = launch(service(repository, reader=reader))
    status, record = client.gmail({"query": "from:x", "labels": ["INBOX"], "limit": 7})
    assert status == 200, record
    assert reader.calls == ["verify", ("messages", "from:x", ("INBOX",), 7)]
    # The bounds the request named reached the reader exactly, and nothing else did.
    assert record["mailbox"] == NAMESPACE


def test_a_mailbox_that_cannot_be_proven_is_never_read(launch, repository):
    """Nothing is fetched, so there is nothing to decide whether to keep."""
    reader = Recording(proven="gmail:someone-else@example.com")
    client = launch(service(repository, reader=reader))
    before = stored(repository)
    status, body = client.gmail()
    assert status == 502, body
    assert reader.calls == ["verify"], reader.calls
    assert stored(repository) == before


@pytest.mark.parametrize(
    "payload",
    [
        {"limit": 0},
        {"limit": -1},
        {"limit": 101},
        {"limit": True},
        {"limit": False},
        {"limit": 1.0},
        {"limit": "25"},
        {"labels": "INBOX"},
        {"labels": [1]},
        {"labels": {"id": "INBOX"}},
        {"query": 5},
        {"query": None},
    ],
    ids=[
        "zero",
        "negative",
        "over the ceiling",
        "true is not a count",
        "false is not a count",
        "a float is not a count",
        "text is not a count",
        "a string pretending to be a sequence",
        "a label that is not text",
        "an object of labels",
        "a query that is not text",
        "a null query",
    ],
)
def test_a_gmail_command_this_surface_cannot_mean_is_refused(launch, repository, payload):
    mailbox = Mailbox({"aaaa1111": eml()})
    client = launch(service(repository, reader=reading(mailbox)))
    before = stored(repository)
    status, body = client.gmail(payload)
    assert status == 400, body
    # Refused before anything was contacted, so a malformed command spends no credential.
    assert mailbox.asked == []
    assert stored(repository) == before


@pytest.mark.parametrize(
    "field", ["mailbox", "token", "namespace", "skills", "locations", "provider", "profile"]
)
def test_the_launch_owns_every_fact_a_request_might_try_to_name(launch, repository, field):
    """Refused rather than ignored. A caller that sent `mailbox` believed it was honoured,
    and the one answer worse than refusing it is reading a different mailbox silently."""
    mailbox = Mailbox({"aaaa1111": eml()})
    client = launch(service(repository, reader=reading(mailbox)))
    before = stored(repository)
    status, body = client.gmail({field: "whatever"})
    assert status == 400, body
    assert field in body["error"]
    assert mailbox.asked == []
    assert stored(repository) == before


def test_a_gmail_body_that_is_not_an_object_is_refused(launch, repository):
    mailbox = Mailbox({"aaaa1111": eml()})
    client = launch(service(repository, reader=reading(mailbox)))
    for raw in (b"[]", b'"limit"', b"5", b"null", b"{oops"):
        status, body = client.send(
            "/api/v1/intake/gmail", content_type="application/json", body=raw
        )
        assert status == 400, (raw, status, body)
    assert mailbox.asked == []


def test_a_mailbox_that_is_not_the_declared_one_is_refused_before_a_message_is_stored(
    launch, repository
):
    """`--mailbox` is a declaration; the credential is the fact.

    A token belonging to somebody else would otherwise have every message it returned
    recorded under a namespace it does not answer to.
    """
    mailbox = Mailbox({"aaaa1111": eml()}, identity="someone-else@example.com")
    client = launch(service(repository, reader=reading(mailbox)))
    before = stored(repository)
    status, body = client.gmail()
    assert status == 502, body
    assert body["error"] == "source_unreadable"
    assert stored(repository) == before
    # The only request made was the identity proof; no message was ever asked for.
    assert [url for url in mailbox.asked if "/messages" in url] == []


@pytest.mark.parametrize("code", [401, 403, 429, 500])
def test_a_mailbox_that_cannot_be_read_writes_nothing_and_says_so(launch, repository, code):
    mailbox = Mailbox({"aaaa1111": eml()}, fail=code)
    client = launch(service(repository, reader=reading(mailbox)))
    before = stored(repository)
    status, body = client.gmail()
    assert status == 502, body
    assert body["error"] == "source_unreadable"
    assert stored(repository) == before


def test_a_read_failure_never_carries_the_credential(launch, repository):
    """The adapter puts no token in a message, and nothing here adds one back."""
    mailbox = Mailbox({"aaaa1111": eml()}, fail=401)
    client = launch(service(repository, reader=reading(mailbox)))
    status, body = client.gmail()
    assert status == 502
    printed = json.dumps(body)
    assert "synthetic-read-token" not in printed
    assert "Authorization" not in printed and "Bearer" not in printed


def test_a_batch_that_fails_partway_leaves_nothing_from_that_batch_behind(launch, repository):
    """The property the whole ordering exists for.

    The first message here reads perfectly well. The second does not. If intake happened as
    each message arrived, the first would already be stored when the second failed -- a
    partial import nobody asked for, under a report that says the read failed.
    """
    mailbox = Mailbox(
        {"aaaa1111": eml(), "bbbb2222": eml(body=SECOND_JOB, header_id="<second@example.com>")},
        unreadable={"bbbb2222"},
    )
    client = launch(service(repository, reader=reading(mailbox)))
    before = stored(repository)
    status, body = client.gmail()
    assert status == 502, body
    # The readable message really was fetched, so this is about ordering and not about luck.
    assert any("aaaa1111" in url for url in mailbox.asked), mailbox.asked
    assert stored(repository) == before
    assert repository.communications() == []
    assert repository.opportunities() == []


def test_importing_the_same_gmail_message_twice_creates_nothing_the_second_time(launch, repository):
    mailbox = Mailbox({"aaaa1111": eml()})
    client = launch(service(repository, reader=reading(mailbox)))
    first_status, first = client.gmail()
    second_status, second = client.gmail()
    assert (first_status, second_status) == (200, 200)
    assert second["messages"][0]["message"] == first["messages"][0]["message"]
    assert second["messages"][0]["reviews"] == first["messages"][0]["reviews"]
    assert len(repository.communications()) == 1
    assert len(repository.opportunities()) == 1


def test_the_reader_this_surface_is_given_can_only_read(launch, repository):
    """Every URL the transport was asked for is an allowlisted read.

    The read adapter defines no send, draft, modify, label, trash or watch operation at all,
    and this is the runtime half of that claim: whatever a browser request became, it became
    requests to the messages collection and the profile endpoint and nothing else.
    """
    mailbox = Mailbox({"aaaa1111": eml()})
    client = launch(service(repository, reader=reading(mailbox)))
    assert client.gmail()[0] == 200
    assert mailbox.asked
    for url in mailbox.asked:
        assert url.startswith("https://gmail.googleapis.com/gmail/v1/users/me/")
        tail = url[len("https://gmail.googleapis.com/gmail/v1/users/me/") :]
        assert tail.split("?")[0].split("/")[0] in ("messages", "profile"), url
        for operation in ("send", "drafts", "modify", "trash", "batchModify", "watch", "labels"):
            assert f"/{operation}" not in url, url


def test_gmail_intake_never_reaches_the_compose_credential(launch, repository, monkeypatch):
    """Read authority and draft authority are separate grants, and stay separate.

    A compose token in the environment is not an input to intake: this launch was handed a
    read credential, that is the only credential the service holds, and no import can turn
    one into the other.
    """
    monkeypatch.setenv(COMPOSE_TOKEN_VARIABLE, "ya29-not-a-real-compose-credential")
    mailbox = Mailbox({"aaaa1111": eml()})
    client = launch(service(repository, reader=reading(mailbox)))
    assert client.gmail()[0] == 200
    for url in mailbox.asked:
        assert "draft" not in url.casefold()
    # And nothing was drafted, claimed or attempted by taking a message in.
    assert all(repository.intent(row["review"]) is None for row in repository.opportunities())


def test_a_query_string_on_the_gmail_address_is_refused(launch, repository):
    mailbox = Mailbox({"aaaa1111": eml()})
    client = launch(service(repository, reader=reading(mailbox)))
    status, payload = client.send(
        "/api/v1/intake/gmail?limit=100", content_type="application/json", body=b"{}"
    )
    assert status == 400, payload
    assert mailbox.asked == []


def test_an_oversized_gmail_command_is_refused_on_what_it_declares(launch, repository):
    mailbox = Mailbox({"aaaa1111": eml()})
    client = launch(service(repository, reader=reading(mailbox)))
    status, body = client.gmail({"query": "x"}, length=web.MAX_BODY + 1)
    assert status == 413, body
    assert mailbox.asked == []


# --- what the launch decides, and the request never does ------------------------------------


def test_a_launch_with_no_profile_takes_nothing_in(launch, repository):
    """No `--skill` means no profile to score against, so there is no intake authority."""
    client = launch(None)
    before = stored(repository)
    status, sources = client.read("/api/v1/intake/sources")
    assert (status, sources) == (200, web.NO_INTAKE)
    for status, body in (client.eml(eml()), client.gmail()):
        assert status == 409, body
        assert body["error"] == "intake_unavailable"
    assert stored(repository) == before


def test_a_launch_with_no_profile_settles_that_before_it_looks_at_anything_sent(launch, repository):
    """The capability answer comes first, at both addresses, whatever the request said.

    A launch that takes nothing in should never parse intake input: not a malformed command,
    not a two-megabyte body, not a media type it would otherwise have to judge. Answering 400
    or 413 here would also be answering a question about a request that was never going to be
    acted on, and would tell the operator to fix the wrong thing.
    """
    client = launch(None)
    before = stored(repository)
    for status, body in (
        client.gmail({"skills": ["rust"]}),
        client.gmail({"limit": 0}),
        client.gmail(payload=None, content_type="text/plain"),
        client.eml(b"x", length=MAX_MESSAGE_BYTES + 1),
        client.eml(eml(), namespace=None),
        client.eml(eml(), content_type="application/json"),
    ):
        assert status == 409, body
        assert body["error"] == "intake_unavailable", body
    assert stored(repository) == before


def test_a_launch_with_skills_can_take_a_local_message_in_without_any_credential(
    launch, repository, monkeypatch
):
    """Local intake needs no Gmail grant of either kind, and asks for none."""
    monkeypatch.delenv("CAREERSIGNAL_GMAIL_TOKEN", raising=False)
    monkeypatch.delenv(COMPOSE_TOKEN_VARIABLE, raising=False)
    client = launch(service(repository))
    status, sources = client.read("/api/v1/intake/sources")
    assert sources["eml"]["available"] is True
    assert sources["gmail"] == {"available": False}
    assert client.eml(eml())[0] == 200
    assert client.gmail()[0] == 409


def test_the_launch_profile_is_what_the_sources_report(launch, repository):
    client = launch(service(repository, skills=("SailPoint", "IAM"), locations=("Remote",)))
    status, sources = client.read("/api/v1/intake/sources")
    assert status == 200
    # Normalised by `Profile`, which is the one place that decides what a skill is.
    assert sources["profile"] == {"skills": ["iam", "sailpoint"], "locations": ["remote"]}


def test_locations_default_to_remote_when_the_launch_names_none(launch, repository):
    client = launch(service(repository))
    assert client.read("/api/v1/intake/sources")[1]["profile"]["locations"] == ["remote"]


def test_explicit_launch_locations_are_the_ones_used(launch, repository):
    """And they really govern: an onsite-only launch does not make a remote role eligible."""
    client = launch(launched := service(repository, locations=("onsite new york",)))
    assert client.read("/api/v1/intake/sources")[1]["profile"]["locations"] == ["onsite new york"]
    status, record = client.eml(eml())
    assert status == 200, record
    assert launched.profile.locations == ("onsite new york",)
    assert [row["eligible"] for row in repository.opportunities()] == [False]


def test_the_browser_cannot_replace_the_profile_it_is_judged_against(launch, repository):
    """Every spelling of the attempt, at both addresses, against a real launch.

    A recruiter writes the material. If the request could name the skills, the material could
    choose the policy it is scored under -- which is not material being judged.
    """
    mailbox = Mailbox({"aaaa1111": eml()})
    client = launch(service(repository, skills=("python", "sql"), reader=reading(mailbox)))
    for payload in ({"skills": ["rust"]}, {"locations": ["onsite berlin"]}, {"profile": {}}):
        assert client.gmail(payload)[0] == 400, payload
    assert client.eml(eml(), content_type="application/json")[0] == 415
    # The launch profile is unchanged, and so is what it decides.
    assert client.read("/api/v1/intake/sources")[1]["profile"]["skills"] == ["python", "sql"]


def test_the_sources_read_contacts_no_mailbox(launch, repository):
    """Opening the page must not spend a credential, and a read of local configuration must
    not depend on whether Google is reachable."""
    mailbox = Mailbox({"aaaa1111": eml()}, fail=401)
    client = launch(service(repository, reader=reading(mailbox)))
    status, sources = client.read("/api/v1/intake/sources")
    assert status == 200, sources
    assert sources["gmail"] == {"available": True, "namespace": NAMESPACE}
    assert mailbox.asked == [], "answering the discovery read reached the network"


def test_the_sources_read_reports_no_credential_of_any_kind(launch, repository):
    mailbox = Mailbox({"aaaa1111": eml()})
    client = launch(service(repository, reader=reading(mailbox)))
    printed = json.dumps(client.read("/api/v1/intake/sources")[1])
    assert "synthetic-read-token" not in printed
    assert "token" not in printed.casefold()
    assert "scope" not in printed.casefold()
    assert str(len("synthetic-read-token")) not in printed


def test_the_intake_addresses_need_the_same_provenance_every_command_needs(launch, repository):
    mailbox = Mailbox({"aaaa1111": eml()})
    client = launch(service(repository, reader=reading(mailbox)))
    before = stored(repository)
    assert client.eml(eml(), origin=False)[0] == 403
    assert client.eml(eml(), origin="http://evil.example")[0] == 403
    assert client.eml(eml(), token=False)[0] == 401
    assert client.eml(eml(), token="wrong")[0] == 401
    assert client.gmail(origin=False)[0] == 403
    assert client.gmail(token=False)[0] == 401
    assert stored(repository) == before
    assert mailbox.asked == []


@pytest.mark.parametrize("address", ["/api/v1/intake/eml", "/api/v1/intake/gmail"])
def test_reading_a_command_address_is_not_a_route(client, address):
    """Exactly as the outward addresses answer: the read router has no such route."""
    for method in ("GET", "HEAD"):
        status, _ = client.send(address, method=method, origin=False, body=None)
        assert status == 404, (method, address)


@pytest.mark.parametrize("method", ["PUT", "PATCH", "DELETE", "FROBNICATE"])
def test_no_other_verb_reaches_the_intake_addresses(client, repository, method):
    before = stored(repository)
    status, _ = client.send("/api/v1/intake/eml", method=method, body=None)
    assert status == 405
    assert stored(repository) == before


@pytest.mark.parametrize(
    "address",
    [
        "/api/v1/intake",
        "/api/v1/intake/",
        "/api/v1/intake/sources/eml",
        "/api/v1/intake/EML",
        "/api/v1/intake/mbox",
        "/api/v1/intake/imap",
    ],
)
def test_there_is_no_generic_intake_address(client, repository, address):
    """Each authority is named by its own address. There is nothing that takes a source."""
    before = stored(repository)
    status, _ = client.send(
        address, content_type="application/json", body=json.dumps({"source": "gmail"}).encode()
    )
    assert status in (404, 405), address
    assert stored(repository) == before


# --- one intake, two surfaces ----------------------------------------------------------------


def test_the_browser_and_the_command_line_take_the_same_message_in_identically(
    launch, tmp_path, monkeypatch, capsys
):
    """The parity that keeps the web from becoming a second scorer.

    Same bytes, same namespace, same profile, two surfaces. Everything an operator or a later
    command can see -- the reviews, the coverage, the eligibility, the evidence, the
    provenance -- has to come out the same, because it is the same code doing it.
    """
    raw = eml()
    through_browser = Repository(tmp_path / "browser.db")
    client = launch(service(through_browser))
    status, record = client.eml(raw, namespace="archive:parity")
    assert status == 200, record

    message = tmp_path / "parity.eml"
    message.write_bytes(raw)
    path = tmp_path / "terminal.db"
    monkeypatch.setattr(
        "sys.argv",
        [
            "careersignal",
            "ingest",
            "--db",
            str(path),
            "--message",
            str(message),
            "--namespace",
            "archive:parity",
            "--skill",
            "python",
            "--skill",
            "sql",
        ],
    )
    from system import cli

    cli.main()
    printed = json.loads(capsys.readouterr().out)
    through_terminal = Repository(path)

    assert printed["reviews"] == record["reviews"]
    assert printed["diagnostics"] == record["diagnostics"]
    assert through_terminal.opportunities() == through_browser.opportunities()
    assert through_terminal.communications() == through_browser.communications()
    for row in through_browser.opportunities():
        assert through_terminal.opportunity(row["id"]) == through_browser.opportunity(row["id"])


def test_the_command_line_still_prints_exactly_the_fields_it_always_printed(
    tmp_path, monkeypatch, capsys
):
    """Sharing the orchestration must not widen an established output contract.

    The service reports more than these commands print -- a namespace, a message key, per
    message diagnostics. Scripts read this output, so what each surface says about a shared
    orchestration stays each surface's own.
    """
    message = tmp_path / "one.eml"
    message.write_bytes(eml())
    monkeypatch.setattr(
        "sys.argv",
        [
            "careersignal",
            "ingest",
            "--db",
            str(tmp_path / "db"),
            "--message",
            str(message),
            "--namespace",
            "archive:one",
            "--skill",
            "python",
        ],
    )
    from system import cli

    cli.main()
    printed = json.loads(capsys.readouterr().out)
    assert sorted(printed) == ["diagnostics", "reviews"]
    assert sorted(printed["diagnostics"][0]) == ["item", "reason", "review"]


def test_gmail_ingest_still_prints_exactly_the_fields_it_always_printed(
    tmp_path, monkeypatch, capsys
):
    mailbox = Mailbox({"aaaa1111": eml()})
    monkeypatch.setenv("CAREERSIGNAL_GMAIL_TOKEN", "synthetic-read-token")
    monkeypatch.setattr(
        "system.cli.GmailReader", lambda credentials: GmailReader(credentials, mailbox)
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "careersignal",
            "gmail-ingest",
            "--db",
            str(tmp_path / "db"),
            "--mailbox",
            MAILBOX,
            "--skill",
            "python",
        ],
    )
    from system import cli

    cli.main()
    printed = json.loads(capsys.readouterr().out)
    assert sorted(printed) == ["mailbox", "messages", "read"]
    assert printed["mailbox"] == NAMESPACE
    assert sorted(printed["messages"][0]) == ["external_id", "message", "reviews"]


def test_gmail_ingest_still_proves_the_mailbox_before_the_database_is_touched(
    tmp_path, monkeypatch
):
    """The early check the command has always made, kept where it was.

    `Repository(path)` runs migrations, so a mismatched token must be caught before it is
    constructed -- not merely before the first message is stored.
    """
    mailbox = Mailbox({"aaaa1111": eml()}, identity="someone-else@example.com")
    monkeypatch.setenv("CAREERSIGNAL_GMAIL_TOKEN", "synthetic-read-token")
    monkeypatch.setattr(
        "system.cli.GmailReader", lambda credentials: GmailReader(credentials, mailbox)
    )
    path = tmp_path / "never.db"
    monkeypatch.setattr(
        "sys.argv",
        [
            "careersignal",
            "gmail-ingest",
            "--db",
            str(path),
            "--mailbox",
            MAILBOX,
            "--skill",
            "python",
        ],
    )
    from system import cli

    with pytest.raises(GmailError):
        cli.main()
    assert not path.exists(), "the database was created before the mailbox was proven"


def test_serve_without_a_skill_has_no_intake_authority(monkeypatch, capsys, tmp_path):
    """Decided at the composition root, where every other authority for this launch is."""
    served = []
    monkeypatch.setattr(
        "system.cli.serve",
        lambda repository, **kwargs: served.append(kwargs),
    )
    monkeypatch.setattr("sys.argv", ["careersignal", "serve", "--db", str(tmp_path / "db")])
    from system import cli

    cli.main()
    assert served[0]["inbound"] is None
    capsys.readouterr()


def test_serve_with_a_skill_has_local_intake_and_no_mailbox_without_a_read_token(
    monkeypatch, capsys, tmp_path
):
    monkeypatch.delenv("CAREERSIGNAL_GMAIL_TOKEN", raising=False)
    served = []
    monkeypatch.setattr(
        "system.cli.serve",
        lambda repository, **kwargs: served.append(kwargs),
    )
    monkeypatch.setattr(
        "sys.argv",
        ["careersignal", "serve", "--db", str(tmp_path / "db"), "--skill", "python"],
    )
    from system import cli

    cli.main()
    inbound = served[0]["inbound"]
    assert inbound is not None
    assert inbound.available() == {
        "eml": {"available": True},
        "gmail": {"available": False},
        "profile": {"skills": ["python"], "locations": ["remote"]},
    }
    capsys.readouterr()


def test_serve_with_a_read_token_and_a_mailbox_has_gmail_intake(monkeypatch, capsys, tmp_path):
    """The read grant, never the compose one: they are different credentials."""
    monkeypatch.setenv("CAREERSIGNAL_GMAIL_TOKEN", "synthetic-read-token")
    monkeypatch.delenv(COMPOSE_TOKEN_VARIABLE, raising=False)
    served = []
    monkeypatch.setattr(
        "system.cli.serve",
        lambda repository, **kwargs: served.append(kwargs),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "careersignal",
            "serve",
            "--db",
            str(tmp_path / "db"),
            "--skill",
            "python",
            "--mailbox",
            MAILBOX,
        ],
    )
    from system import cli

    cli.main()
    found = served[0]["inbound"].available()
    assert found["gmail"] == {"available": True, "namespace": NAMESPACE}
    # Drafting is a separate authority and this launch does not have it.
    assert served[0]["actions"] is not None  # the controlled provider is local
    capsys.readouterr()


def test_a_compose_credential_alone_gives_no_intake_authority(monkeypatch, capsys, tmp_path):
    """The independence stated as behaviour: one grant is not the other."""
    monkeypatch.delenv("CAREERSIGNAL_GMAIL_TOKEN", raising=False)
    monkeypatch.setenv(COMPOSE_TOKEN_VARIABLE, "ya29-not-a-real-compose-credential")
    served = []
    monkeypatch.setattr(
        "system.cli.serve",
        lambda repository, **kwargs: served.append(kwargs),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "careersignal",
            "serve",
            "--db",
            str(tmp_path / "db"),
            "--skill",
            "python",
            "--provider",
            "gmail",
            "--mailbox",
            MAILBOX,
        ],
    )
    from system import cli

    cli.main()
    assert served[0]["inbound"].available()["gmail"]["available"] is False
    assert served[0]["actions"] is not None, "the compose credential still grants drafting"
    capsys.readouterr()


def test_the_launch_says_what_it_can_take_in(repository):
    """An operator who cannot see this from the terminal finds out by pressing a button that
    is not there -- so the announcement reads the same answer the discovery route reads."""
    assert web.sources(service(repository)) == ["eml"]
    assert web.sources(service(repository, reader=reading(Mailbox({})))) == ["eml", "gmail"]


def test_what_the_terminal_announces_matches_what_the_browser_is_offered(repository, launch):
    """One answer, two audiences. A line that said Gmail while the page withheld the control
    would leave the operator debugging a mailbox that was never configured."""
    for reader in (None, reading(Mailbox({}))):
        inbound = service(repository, reader=reader)
        client = launch(inbound)
        offered = client.read("/api/v1/intake/sources")[1]
        announced = web.sources(inbound)
        assert announced == [name for name in ("eml", "gmail") if offered[name]["available"]]
