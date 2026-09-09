"""The refusals are written first: a read-only adapter is defined by what it cannot do."""

import base64
import json
import traceback

import pytest

from communications import gmail
from communications.gmail import (
    GMAIL_HOST,
    READONLY_SCOPE,
    GmailCredentials,
    GmailError,
    GmailReader,
    readable_url,
)

TOKEN = "synthetic-access-token-value"
MAILBOX = "operator@example.com"
JOB_TEXT = (
    "Title: Application Engineer\r\n"
    "Company: Example Company\r\n"
    "Location: remote\r\n"
    "Skills: Python, SQL\r\n"
    "URL: https://jobs.example.com/roles/1\r\n"
)


def credentials(**overrides):
    return GmailCredentials(**{"access_token": TOKEN, "mailbox": MAILBOX, **overrides})


def raw_bytes(*, subject="Job alert", sender="alerts@example.com", header_id="<a@example.com>"):
    return (
        f"From: {sender}\r\n"
        f"To: {MAILBOX}\r\n"
        f"Subject: {subject}\r\n"
        f"Message-ID: {header_id}\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: text/plain; charset="utf-8"\r\n'
        "\r\n"
        f"{JOB_TEXT}"
    ).encode()


def encoded(raw: bytes) -> str:
    """Gmail returns unpadded base64url, which is the form the decoder must accept."""
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class Recorder:
    """Serves recorded synthetic Gmail payloads and records every request it is given."""

    def __init__(self, pages=(), bodies=None, status=200):
        self.pages = list(pages)
        self.bodies = dict(bodies or {})
        self.status = status
        self.requests = []

    def __call__(self, url, headers):
        self.requests.append((url, dict(headers)))
        if self.status != 200:
            return self.status, b'{"error": {"message": "synthetic failure"}}'
        if "/messages/" in url:
            identifier = url.split("/messages/", 1)[1].split("?", 1)[0]
            payload = self.bodies.get(identifier)
            if payload is None:
                return 404, b"{}"
            return 200, json.dumps(payload).encode()
        page = self.pages.pop(0) if self.pages else {}
        return 200, json.dumps(page).encode()


def reader(pages=(), bodies=None, **overrides):
    transport = Recorder(pages, bodies)
    return GmailReader(credentials(**overrides), transport=transport), transport


def one_message(identifier="1111aaaa2222bbbb", **message):
    return (
        [{"messages": [{"id": identifier}]}],
        {identifier: {"id": identifier, "raw": encoded(raw_bytes(**message))}},
    )


# --- Refusals -------------------------------------------------------------------------

REFUSED_SCOPES = [
    ("https://www.googleapis.com/auth/gmail.modify",),
    ("https://www.googleapis.com/auth/gmail.send",),
    ("https://www.googleapis.com/auth/gmail.compose",),
    ("https://mail.google.com/",),
    (READONLY_SCOPE, "https://www.googleapis.com/auth/gmail.send"),
    ("https://www.googleapis.com/auth/gmail.readonly.extra",),
    (),
]


@pytest.mark.parametrize("scopes", REFUSED_SCOPES)
def test_only_the_exact_read_scope_may_be_configured(scopes):
    """A broader grant can only be a configuration mistake: this program has no write path."""
    with pytest.raises(ValueError, match="gmail.readonly"):
        credentials(scopes=scopes)


def test_the_read_scope_is_accepted():
    assert credentials().scopes == (READONLY_SCOPE,)
    assert credentials(scopes=[READONLY_SCOPE]).scopes == (READONLY_SCOPE,)


# Every Gmail mutation is addressed through one of these segments. None may be reachable.
WRITE_SEGMENTS = [
    "send",
    "import",
    "insert",
    "batchDelete",
    "batchModify",
    "trash",
    "untrash",
    "modify",
    "drafts",
    "settings",
    "watch",
    "stop",
]


@pytest.mark.parametrize("segment", WRITE_SEGMENTS)
def test_a_write_endpoint_can_never_be_addressed(segment):
    """A hostile or malformed identifier must not be spellable into a mutation URL."""
    client, transport = reader()
    with pytest.raises(GmailError, match="Refused|unusable"):
        client.fetch(segment)
    assert transport.requests == []


FOREIGN_URLS = [
    "https://evil.example.com/gmail/v1/users/me/messages",
    "https://gmail.googleapis.com.evil.example.com/gmail/v1/users/me/messages",
    "http://gmail.googleapis.com/gmail/v1/users/me/messages",
    "https://gmail.googleapis.com:8443/gmail/v1/users/me/messages",
    # Userinfo is spelled without an email shape so the tree scanner stays strict.
    "https://user:@gmail.googleapis.com/gmail/v1/users/me/messages",
    "https://:secret!@gmail.googleapis.com/gmail/v1/users/me/messages",
    "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
    "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
    "https://gmail.googleapis.com/gmail/v1/users/me/messages/abc/modify",
    "https://gmail.googleapis.com/gmail/v1/users/me/settings/forwarding",
    "https://gmail.googleapis.com/upload/gmail/v1/users/me/messages/send",
    "https://www.googleapis.com/gmail/v1/users/me/messages",
]


@pytest.mark.parametrize("url", FOREIGN_URLS)
def test_only_an_allowlisted_gmail_read_url_survives_the_guard(url):
    with pytest.raises(GmailError, match="Refused"):
        readable_url(url)


def test_the_allowlisted_read_urls_survive():
    root = f"https://{GMAIL_HOST}/gmail/v1/users/me/messages"
    assert readable_url(root) == root
    assert readable_url(f"{root}?q=label%3Ajobs") == f"{root}?q=label%3Ajobs"
    assert readable_url(f"{root}/1111aaaa2222bbbb?format=raw")


UNUSABLE_IDENTIFIERS = ["", "   ", "a/b", "../drafts", "a?format=raw", "a#b", "x" * 129, "a b"]


@pytest.mark.parametrize("identifier", UNUSABLE_IDENTIFIERS)
def test_an_unusable_message_identifier_is_refused_before_any_request(identifier):
    client, transport = reader()
    with pytest.raises(GmailError, match="unusable"):
        client.fetch(identifier)
    assert transport.requests == []


@pytest.mark.parametrize("identifier", [None, 17, b"abc", ["abc"]])
def test_a_non_text_message_identifier_is_refused(identifier):
    client, _ = reader()
    with pytest.raises(GmailError, match="unusable"):
        client.fetch(identifier)


FORBIDDEN_METHODS = [
    "create",
    "send",
    "lookup",
    "modify",
    "trash",
    "delete",
    "update",
    "insert",
    "draft",
    "drafts",
    "reply",
    "forward",
    "label",
]


@pytest.mark.parametrize("name", FORBIDDEN_METHODS)
def test_the_reader_exposes_no_write_capability(name):
    """The adapter must not accidentally satisfy the draft provider interface."""
    assert not hasattr(GmailReader, name)


def test_the_only_network_helper_issues_a_get_and_refuses_a_redirect(monkeypatch):
    captured = {}

    class Response:
        status = 200

        def read(self, size):
            return b"{}"

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    # The signature mirrors OpenerDirector.open, whose second positional parameter is the
    # request body. A fake that accepts the timeout positionally would hide a body being sent.
    def fake_open(fullurl, data=None, timeout=None):
        captured["method"] = fullurl.get_method()
        captured["url"] = fullurl.full_url
        captured["body"] = fullurl.data
        captured["data"] = data
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(gmail._OPENER, "open", fake_open)
    gmail.https_get(
        f"https://{GMAIL_HOST}/gmail/v1/users/me/messages", {"Authorization": "Bearer x"}
    )
    assert captured == {
        "method": "GET",
        "url": f"https://{GMAIL_HOST}/gmail/v1/users/me/messages",
        "body": None,
        "data": None,
        "timeout": gmail.NETWORK_TIMEOUT,
    }
    # A redirect would re-send the bearer token to whatever host the response named.
    with pytest.raises(GmailError, match="redirect"):
        gmail._NoRedirect().redirect_request(
            None, None, 302, "Found", {}, "https://evil.example.com"
        )


# --- The access token must not escape ------------------------------------------------


def test_the_token_never_appears_in_a_url():
    client, transport = reader(*one_message())
    client.messages()
    assert transport.requests
    for url, headers in transport.requests:
        assert TOKEN not in url
        assert headers["Authorization"] == "Bearer " + TOKEN


def test_the_token_is_redacted_from_the_credentials_representation():
    value = credentials()
    assert TOKEN not in repr(value) and TOKEN not in str(value)
    assert "redacted" in repr(value) and MAILBOX in repr(value)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_a_failed_read_reports_the_status_without_echoing_token_or_body(status):
    client = GmailReader(credentials(), transport=Recorder(status=status))
    with pytest.raises(GmailError) as failure:
        client.identifiers()
    assert TOKEN not in str(failure.value)
    assert "synthetic failure" not in str(failure.value)
    assert str(status) in str(failure.value)


def test_an_expired_token_says_how_to_recover():
    client = GmailReader(credentials(), transport=Recorder(status=401))
    with pytest.raises(GmailError, match="reauthorize"):
        client.identifiers()


# --- Provenance -----------------------------------------------------------------------


def test_the_provider_identifier_is_the_provenance_not_the_rfc822_header():
    """A Message-ID header is written by the sender; two messages can carry the same one."""
    pages = [{"messages": [{"id": "aaa1"}, {"id": "bbb2"}]}]
    bodies = {
        "aaa1": {"id": "aaa1", "raw": encoded(raw_bytes(header_id="<same@example.com>"))},
        "bbb2": {
            "id": "bbb2",
            "raw": encoded(raw_bytes(header_id="<same@example.com>", subject="Second alert")),
        },
    }
    first, second = GmailReader(credentials(), transport=Recorder(pages, bodies)).messages()
    assert (first.external_id, second.external_id) == ("aaa1", "bbb2")
    assert first.key != second.key


def test_the_mailbox_names_the_namespace_and_is_case_folded():
    """Two spellings of one mailbox must not split a message into two identities."""
    assert credentials().namespace == "gmail:operator@example.com"
    assert credentials(mailbox=" Operator@Example.COM ").namespace == credentials().namespace
    plain, _ = reader(*one_message())
    mixed = GmailReader(
        credentials(mailbox="Operator@Example.com"), transport=Recorder(*one_message())
    )
    assert plain.messages()[0].key == mixed.messages()[0].key


def test_a_different_mailbox_keeps_an_identical_message_distinct():
    other = GmailReader(
        credentials(mailbox="second@example.com"), transport=Recorder(*one_message())
    )
    client, _ = reader(*one_message())
    assert client.messages()[0].key != other.messages()[0].key


@pytest.mark.parametrize("mailbox", ["", "   ", None])
def test_a_missing_mailbox_is_refused(mailbox):
    with pytest.raises((ValueError, TypeError)):
        credentials(mailbox=mailbox)


@pytest.mark.parametrize("token", ["", None, 17])
def test_a_missing_access_token_is_refused(token):
    with pytest.raises((ValueError, TypeError)):
        credentials(access_token=token)


# A header value is where the token is used, so anything that cannot legally sit in one is
# not a usable token. http.client rejects such a value by quoting the whole header, which
# would put the token in a traceback.
HEADER_UNSAFE_TOKENS = [
    "synthetic\r\nX-Injected: evil",
    "synthetic\nX-Injected: evil",
    "synthetic\r",
    "synthetic\x00value",
    "synthetic value",
    " synthetic",
    "synthetic ",
    "   ",
    "synthetic\tvalue",
    "synthetic\x7f",
    "synthét1c",
]


@pytest.mark.parametrize("token", HEADER_UNSAFE_TOKENS)
def test_a_token_that_cannot_sit_in_a_header_is_refused_without_echoing_it(token):
    with pytest.raises(ValueError) as failure:
        credentials(access_token=token)
    assert token not in str(failure.value)


def test_a_refused_token_never_reaches_a_transport():
    """Construction fails, so no reader exists that could carry the value to a request."""
    transport = Recorder(*one_message())
    with pytest.raises(ValueError):
        GmailReader(credentials(access_token="synthetic\r\nX-Injected: evil"), transport)
    assert transport.requests == []


def test_the_network_helper_refuses_an_illegal_header_without_echoing_it():
    """Defence in depth: the standard library quotes the whole value when it rejects one."""
    poisoned = TOKEN + "\r\nX-Injected: evil"
    with pytest.raises(GmailError) as failure:
        gmail.https_get(
            f"https://{GMAIL_HOST}/gmail/v1/users/me/messages", {"Authorization": poisoned}
        )
    # The whole chain is checked, not just the message: an implicit __context__ would print
    # the original ValueError, and that is what carries the token.
    rendered = "".join(traceback.format_exception(failure.value))
    assert TOKEN not in rendered and "X-Injected" not in rendered


# --- Reading --------------------------------------------------------------------------


def test_a_read_message_carries_its_body_through_to_extraction():
    client, transport = reader(*one_message())
    message = client.messages()[0]
    assert message.namespace == "gmail:operator@example.com"
    assert message.sender == "alerts@example.com"
    assert message.subject == "Job alert"
    assert message.format == "text"
    assert "Title: Application Engineer" in message.content
    assert any("format=raw" in url for url, _ in transport.requests)


def test_pagination_follows_the_page_token_and_collapses_repeats():
    """The mailbox is not a snapshot: one identifier can appear on two pages."""
    pages = [
        {"messages": [{"id": "aaa1"}, {"id": "bbb2"}], "nextPageToken": "page-2"},
        {"messages": [{"id": "bbb2"}, {"id": "ccc3"}]},
    ]
    client = GmailReader(credentials(), transport=Recorder(pages))
    assert client.identifiers() == ("aaa1", "bbb2", "ccc3")


def test_the_limit_bounds_the_read_and_stops_paging():
    pages = [
        {"messages": [{"id": "aaa1"}, {"id": "bbb2"}], "nextPageToken": "page-2"},
        {"messages": [{"id": "ccc3"}]},
    ]
    transport = Recorder(pages)
    assert GmailReader(credentials(), transport=transport).identifiers(limit=1) == ("aaa1",)
    assert len(transport.requests) == 1


def test_a_duplicate_across_pages_does_not_consume_the_limit():
    """A repeat must not spend the budget a distinct message was still waiting for."""
    pages = [
        {"messages": [{"id": "aaa1"}], "nextPageToken": "page-2"},
        {"messages": [{"id": "aaa1"}, {"id": "bbb2"}]},
    ]
    client = GmailReader(credentials(), transport=Recorder(pages))
    assert client.identifiers(limit=2) == ("aaa1", "bbb2")


def test_a_page_of_only_duplicates_keeps_paging():
    pages = [
        {"messages": [{"id": "aaa1"}, {"id": "bbb2"}], "nextPageToken": "page-2"},
        {"messages": [{"id": "bbb2"}, {"id": "aaa1"}], "nextPageToken": "page-3"},
        {"messages": [{"id": "ccc3"}]},
    ]
    transport = Recorder(pages)
    client = GmailReader(credentials(), transport=transport)
    assert client.identifiers(limit=3) == ("aaa1", "bbb2", "ccc3")
    assert len(transport.requests) == 3


def test_unique_identifiers_keep_first_seen_order_across_pages():
    pages = [
        {"messages": [{"id": "ccc3"}, {"id": "aaa1"}], "nextPageToken": "page-2"},
        {"messages": [{"id": "aaa1"}, {"id": "bbb2"}]},
    ]
    client = GmailReader(credentials(), transport=Recorder(pages))
    assert client.identifiers(limit=3) == ("ccc3", "aaa1", "bbb2")


@pytest.mark.parametrize("limit", [0, -1, "5", 1.5, True, None])
def test_a_limit_that_is_not_a_positive_count_is_refused(limit):
    client, transport = reader()
    with pytest.raises(ValueError):
        client.identifiers(limit=limit)
    assert transport.requests == []


def test_an_empty_mailbox_reads_as_no_messages():
    client = GmailReader(credentials(), transport=Recorder([{}]))
    assert client.identifiers() == () and client.messages() == ()


def test_the_query_and_labels_are_encoded_into_the_request():
    transport = Recorder([{}])
    GmailReader(credentials(), transport=transport).identifiers(
        query="from:alerts@example.com subject:job", label_ids=("Label_1", "Label_2")
    )
    url = transport.requests[0][0]
    assert "q=from%3Aalerts%40example.com+subject%3Ajob" in url
    assert "labelIds=Label_1" in url and "labelIds=Label_2" in url


# --- Refusing what Gmail returns ------------------------------------------------------


def test_a_response_naming_a_different_message_is_refused():
    """Answering a different identifier would attach one message's provenance to another."""
    bodies = {"aaa1": {"id": "bbb2", "raw": encoded(raw_bytes())}}
    client = GmailReader(
        credentials(), transport=Recorder([{"messages": [{"id": "aaa1"}]}], bodies)
    )
    with pytest.raises(GmailError, match="different message"):
        client.messages()


@pytest.mark.parametrize(
    "payload",
    [
        {"id": "aaa1"},
        {"id": "aaa1", "raw": ""},
        {"id": "aaa1", "raw": None},
        {"id": "aaa1", "raw": 17},
    ],
)
def test_a_response_without_a_raw_body_is_refused(payload):
    client = GmailReader(
        credentials(), transport=Recorder([{"messages": [{"id": "aaa1"}]}], {"aaa1": payload})
    )
    with pytest.raises(GmailError, match="raw message body"):
        client.messages()


def test_an_undecodable_body_is_refused():
    bodies = {"aaa1": {"id": "aaa1", "raw": "not base64 !!!"}}
    client = GmailReader(
        credentials(), transport=Recorder([{"messages": [{"id": "aaa1"}]}], bodies)
    )
    with pytest.raises(GmailError, match="undecodable"):
        client.messages()


@pytest.mark.parametrize(
    "page",
    [
        {"messages": [{"id": "a/b"}]},
        {"messages": [{"id": "drafts"}]},
        {"messages": [{"id": ""}]},
        {"messages": [{"id": None}]},
        {"messages": [{"nope": "aaa1"}]},
        {"messages": ["aaa1"]},
        {"messages": "aaa1"},
    ],
)
def test_an_unusable_listing_entry_is_refused_rather_than_skipped(page):
    client = GmailReader(credentials(), transport=Recorder([page]))
    with pytest.raises(GmailError):
        client.identifiers()


@pytest.mark.parametrize("body", [b"not json", b"", b"[1,2,3]", b'"text"', b"\xff\xfe"])
def test_a_body_that_is_not_a_json_object_is_refused(body):
    client = GmailReader(credentials(), transport=lambda url, headers: (200, body))
    with pytest.raises(GmailError, match="JSON|unexpected"):
        client.identifiers()


def test_a_single_label_string_is_refused_rather_than_read_character_by_character():
    client, transport = reader()
    with pytest.raises(ValueError, match="sequence"):
        client.identifiers(label_ids="Label_1")
    assert transport.requests == []


def test_a_transport_must_be_supplied_with_real_credentials():
    with pytest.raises(TypeError):
        GmailReader("not-credentials")
