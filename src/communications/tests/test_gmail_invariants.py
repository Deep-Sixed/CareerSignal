"""Invariants over generated Gmail inputs.

Three defects in the adapter reached review, and each was the same shape: a claim written
into the docs, tested along the path that made the claim look true. The claim itself was
never attacked. These tests state the claims as rules over generated families instead.

The families are built to include what the author did not think of -- every control
character rather than the newline that came to mind, every reserved segment in every case
rather than the lowercase spelling -- because the previous generated suite missed a defect
by containing only what its author imagined.

Generation is deterministic and uses only the standard library. Nothing here contacts a
mailbox: requests are intercepted at the opener, below the adapter's own transport seam, so
the real network helper is exercised rather than bypassed.
"""

import base64
import itertools
import json
import string
import traceback
from http import client

import pytest

from communications import gmail
from communications.gmail import (
    GMAIL_HOST,
    IDENTIFIER,
    RESERVED_SEGMENTS,
    GmailCredentials,
    GmailError,
    GmailReader,
)

# Every generated token carries this contiguously, so a rendered traceback can be checked
# for one stable string whatever the surrounding placement is. An earlier version compared
# against the fixed TOKEN, which a third of the family did not contain, so those cases
# asserted nothing.
CANARY = "canary-must-not-appear-in-any-traceback"
TOKEN = f"{CANARY}-0001"
MAILBOX = "operator@example.com"
BODY = (
    "From: alerts@example.com\r\nTo: operator@example.com\r\nSubject: Job alert\r\n"
    "Message-ID: <alert@example.com>\r\nMIME-Version: 1.0\r\n"
    'Content-Type: text/plain; charset="utf-8"\r\n\r\n'
    "Title: Application Engineer\r\nCompany: Example Company\r\n"
    "Location: remote\r\nSkills: Python, SQL\r\nURL: https://jobs.example.com/roles/1\r\n"
).encode()
# The two shapes the adapter is allowed to address, and nothing else.
LIST_URL = f"https://{GMAIL_HOST}/gmail/v1/users/me/messages"
GET_PREFIX = f"{LIST_URL}/"


def credentials(**overrides):
    return GmailCredentials(**{"access_token": TOKEN, "mailbox": MAILBOX, **overrides})


def encoded(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class Opener:
    """Stands in for the urllib opener, below the adapter's own transport seam.

    Recording here rather than at the transport means the real https_get runs, so the
    method and body assertions are about the request the standard library would send.
    """

    def __init__(self, pages=(), bodies=None):
        self.pages = list(pages)
        self.bodies = dict(bodies or {})
        self.requests = []

    class Response:
        def __init__(self, payload):
            self.payload = payload
            self.status = 200

        def read(self, size):
            return self.payload[:size]

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    @staticmethod
    def validate(headers):
        """Run the standard library's own header validation without opening a socket.

        putrequest and putheader only buffer; nothing connects until endheaders. Using the
        real validator rather than imitating it keeps this from testing a copy of the rule,
        and keeps the suite off the network -- an earlier draft of this file reached for the
        real opener and spent 35 seconds trying to dial Gmail.
        """
        # HTTPConnection, not HTTPSConnection: header validation is identical and shared,
        # while the TLS variant builds an SSL context per call and costs seconds over a
        # family this size.
        connection = client.HTTPConnection(GMAIL_HOST)
        connection.putrequest("GET", "/")
        for name, value in headers.items():
            connection.putheader(name, value)

    def open(self, fullurl, data=None, timeout=None):
        self.validate(dict(fullurl.headers))
        self.requests.append(
            {
                "method": fullurl.get_method(),
                "url": fullurl.full_url,
                "body": fullurl.data,
                "data": data,
                "headers": dict(fullurl.headers),
            }
        )
        url = fullurl.full_url
        if url.startswith(GET_PREFIX):
            identifier = url[len(GET_PREFIX) :].split("?", 1)[0]
            payload = self.bodies.get(identifier, {"id": identifier, "raw": encoded(BODY)})
        else:
            payload = self.pages.pop(0) if self.pages else {}
        return self.Response(json.dumps(payload).encode())


@pytest.fixture
def opener(monkeypatch):
    """Installed for the whole family so every generated call is recorded."""
    installed = Opener()
    monkeypatch.setattr(gmail, "_OPENER", installed)
    return installed


def leaked(exc, token) -> list:
    """Every form the supplied value could take in a rendered exception chain.

    The raw text is not enough: a traceback that formats the header with !r shows the
    escaped spelling, so a leak could read as "a\\r\\nb" and pass a raw substring check.
    """
    rendered = "".join(traceback.format_exception(exc))
    forms = {
        CANARY,
        token,
        repr(token)[1:-1],
        token.encode("unicode_escape").decode("ascii", "replace"),
    }
    return sorted(form for form in forms if form and form in rendered)


def report(failures, total):
    return f"{len(failures)} of {total} generated cases, first: {failures[:4]}"


# --- The families ------------------------------------------------------------------------

# Every character a header value may not carry, not only the newline that comes to mind.
UNSAFE_CHARACTERS = (
    [chr(code) for code in range(0x00, 0x21)]
    + [chr(0x7F)]
    + [chr(code) for code in range(0x80, 0x86)]
    + [" ", " ", " ", "﻿", "é", "…"]
)
UNSAFE_TOKENS = [
    placement.format(char=char, canary=CANARY)
    for char in UNSAFE_CHARACTERS
    for placement in (
        "{char}{canary}",
        "{canary}{char}",
        "{canary}{char}evil",
        "pre{char}{canary}",
        "{canary}-mid{char}post",
    )
]
# Reserved segments in every casing the identifier pattern would accept.
RESERVED_SPELLINGS = [
    spelled
    for segment in RESERVED_SEGMENTS
    for spelled in (segment, segment.upper(), segment.capitalize(), segment.swapcase())
]
HOSTILE_IDENTIFIERS = RESERVED_SPELLINGS + [
    "",
    " ",
    "..",
    "../drafts",
    "a/b",
    "a/../send",
    "a%2Fsend",
    "a?format=raw&x=1",
    "a#frag",
    "a b",
    "a\tb",
    "a\nb",
    "a\r\nb",
    "a\x00b",
    "x" * 129,
    "é",
    "a.b",
    "a:b",
    "a@b",
    "//evil.example.com",
    "https://evil.example.com/x",
    "\\send",
    "a;send",
    "a&b",
    "a=b",
    "a+b",
]
# Page layouts: duplicates at every position a duplicate can occupy.
PAGE_LAYOUTS = [
    [["aaa1"], ["aaa1", "bbb2"]],
    [["aaa1", "bbb2"], ["aaa1", "ccc3"]],
    [["aaa1", "aaa1"], ["bbb2"]],
    [["aaa1", "bbb2"], ["bbb2", "aaa1"], ["ccc3"]],
    [["aaa1"], ["aaa1"], ["aaa1"], ["bbb2"]],
    [["aaa1", "bbb2", "ccc3"], ["ccc3", "ddd4"]],
    [["aaa1"], [], ["bbb2"]],
    [[], ["aaa1", "bbb2"]],
    [["aaa1", "bbb2"], ["ccc3", "ddd4"], ["eee5"]],
    [["aaa1", "aaa1", "aaa1"]],
]
PAGINATION_CASES = [(layout, limit) for layout in PAGE_LAYOUTS for limit in (1, 2, 3, 4, 5, 10)]
MAILBOX_SPELLINGS = [
    ("operator@example.com", "OPERATOR@EXAMPLE.COM"),
    ("operator@example.com", "Operator@Example.Com"),
    ("operator@example.com", "  operator@example.com  "),
    ("first.last@example.com", "First.Last@Example.COM"),
]
DISTINCT_MAILBOXES = list(
    itertools.combinations(
        ("operator@example.com", "second@example.com", "operator@example.org", "op@example.net"), 2
    )
)


def pages_for(layout):
    return [
        {"messages": [{"id": i} for i in page]}
        | ({"nextPageToken": f"page-{n + 2}"} if n + 1 < len(layout) else {})
        for n, page in enumerate(layout)
    ]


def unique_in(layout):
    return tuple(dict.fromkeys(i for page in layout for i in page))


# --- Invariants 1 and 2: every request is a GET that carries no body ---------------------


def test_every_generated_request_is_a_get_that_carries_no_body(opener):
    """Exercised through the real network helper, not the injected transport."""
    opener.pages = pages_for([["aaa1", "bbb2"], ["ccc3"]]) * 8
    reader = GmailReader(credentials())
    for query, labels, limit in itertools.product(
        ("", "newer_than:7d", "from:alerts@example.com", 'subject:"job alert" OR label:x'),
        ((), ("Label_1",), ("Label_1", "Label_2")),
        (1, 3, 10),
    ):
        opener.pages = pages_for([["aaa1", "bbb2"], ["ccc3"]])
        reader.messages(query=query, label_ids=labels, limit=limit)
    assert opener.requests, "no request was recorded; the family would pass vacuously"
    failures = [
        (r["method"], r["url"], r["body"], r["data"])
        for r in opener.requests
        if r["method"] != "GET" or r["body"] is not None or r["data"] is not None
    ]
    assert not failures, report(failures, len(opener.requests))


def test_every_generated_request_addresses_only_an_allowlisted_read(opener):
    opener.pages = pages_for([["aaa1", "bbb2"], ["ccc3"]])
    GmailReader(credentials()).messages(query="label:jobs", label_ids=("L1",), limit=5)
    failures = [
        r["url"]
        for r in opener.requests
        if not (
            r["url"] == LIST_URL
            or r["url"].startswith(f"{LIST_URL}?")
            or (
                r["url"].startswith(GET_PREFIX)
                and IDENTIFIER.match(r["url"][len(GET_PREFIX) :].split("?", 1)[0])
            )
        )
    ]
    assert not failures, report(failures, len(opener.requests))


# --- Invariants 3 and 4: pagination neither over- nor under-reads -------------------------


def test_a_duplicate_entry_never_consumes_the_unique_message_limit():
    failures = []
    for layout, limit in PAGINATION_CASES:
        reader = GmailReader(
            credentials(),
            transport=lambda url, headers, p=iter(pages_for(layout)): (
                200,
                json.dumps(next(p, {})).encode(),
            ),
        )
        found = reader.identifiers(limit=limit)
        expected = unique_in(layout)[:limit]
        if found != expected:
            failures.append((layout, limit, found, expected))
    assert not failures, report(failures, len(PAGINATION_CASES))


def test_every_unique_message_offered_within_the_page_budget_is_returned():
    """With a limit at least as large as the mailbox, nothing offered may be dropped."""
    failures = []
    for layout in PAGE_LAYOUTS:
        reader = GmailReader(
            credentials(),
            transport=lambda url, headers, p=iter(pages_for(layout)): (
                200,
                json.dumps(next(p, {})).encode(),
            ),
        )
        found = reader.identifiers(limit=gmail.MAX_RESULTS)
        if found != unique_in(layout):
            failures.append((layout, found, unique_in(layout)))
    assert not failures, report(failures, len(PAGE_LAYOUTS))


def test_a_returned_listing_never_repeats_and_never_exceeds_its_limit():
    failures = []
    for layout, limit in PAGINATION_CASES:
        reader = GmailReader(
            credentials(),
            transport=lambda url, headers, p=iter(pages_for(layout)): (
                200,
                json.dumps(next(p, {})).encode(),
            ),
        )
        found = reader.identifiers(limit=limit)
        offered = unique_in(layout)
        if len(set(found)) != len(found) or len(found) > limit or set(found) - set(offered):
            failures.append((layout, limit, found))
    assert not failures, report(failures, len(PAGINATION_CASES))


# --- Invariant 5: no token substring reaches a rendered exception chain -------------------


def test_no_unsafe_token_is_ever_accepted():
    failures = [token for token in UNSAFE_TOKENS if not _refused(token)]
    assert not failures, report(failures, len(UNSAFE_TOKENS))


def _refused(token) -> bool:
    try:
        credentials(access_token=token)
    except ValueError:
        return True
    return False


def test_no_token_substring_appears_in_any_rendered_exception_chain():
    """The whole chain is rendered: a chained cause prints even when the message is clean."""
    failures, refusals = [], 0
    for token in UNSAFE_TOKENS:
        try:
            credentials(access_token=token)
        except ValueError as exc:
            refusals += 1
            found = leaked(exc, token)
            if found:
                failures.append((token, found))
    assert not failures, report(failures, len(UNSAFE_TOKENS))
    assert refusals == len(UNSAFE_TOKENS), f"only {refusals} of {len(UNSAFE_TOKENS)} refused"


def test_the_network_helper_never_renders_a_header_value_it_was_handed(opener):
    """The fake opener runs the standard library's real header validation, so a rejection
    here is the library's, not an imitation of it, and no socket is ever opened.
    """
    failures, refusals = [], 0
    for token in UNSAFE_TOKENS:
        try:
            gmail.https_get(LIST_URL, {"Authorization": f"Bearer {token}"})
        except GmailError as exc:
            refusals += 1
            found = leaked(exc, token)
            if found:
                failures.append((token, found))
        except Exception as exc:  # noqa: BLE001 - any other escape is itself the failure
            failures.append((token, type(exc).__name__, str(exc)[:60]))
    assert not failures, report(failures, len(UNSAFE_TOKENS))
    # Without this the test would pass if every token were somehow accepted and no request
    # ever refused, which is the vacuous outcome it exists to rule out.
    assert refusals > 0, "no header was rejected; the family exercised nothing"


def test_the_leak_detector_catches_a_leaking_helper_for_every_generated_token():
    """Without this the invariants above would pass by never raising, not by never leaking.

    Each case simulates the failure they exist to rule out: the standard library's own
    rejection, which quotes the whole header value, escaping outward through the helper.
    """
    missed = []
    for token in UNSAFE_TOKENS:
        try:
            raise ValueError(f"Invalid header value {('Bearer ' + token).encode()!r}")
        except ValueError as exc:
            if not leaked(exc, token):
                missed.append(token)
    assert not missed, report(missed, len(UNSAFE_TOKENS))


def test_the_leak_detector_sees_a_chained_cause_and_not_a_dropped_one():
    """`from None` is the whole reason the helper is safe; the detector must depend on it."""
    token = UNSAFE_TOKENS[0]
    original = ValueError(f"Invalid header value {('Bearer ' + token)!r}")
    try:
        try:
            raise original
        except ValueError as exc:
            raise GmailError("headers rejected") from exc
    except GmailError as chained:
        assert leaked(chained, token), "a chained cause is printed and must be seen"
    try:
        try:
            raise original
        except ValueError:
            raise GmailError("headers rejected") from None
    except GmailError as dropped:
        assert not leaked(dropped, token), "a dropped cause must not be rendered"


# --- Invariant 9: a hostile identifier is never turned into a writable endpoint -----------


def test_a_hostile_identifier_is_refused_or_addressed_as_an_allowlisted_read(opener):
    failures, refused = [], 0
    for identifier in HOSTILE_IDENTIFIERS:
        opener.requests.clear()
        reader = GmailReader(credentials())
        try:
            reader.fetch(identifier)
        except GmailError:
            refused += 1
            if opener.requests:
                failures.append((identifier, "refused but still issued a request"))
            continue
        for recorded in opener.requests:
            path = recorded["url"][len(GET_PREFIX) :].split("?", 1)[0]
            if not recorded["url"].startswith(GET_PREFIX) or not IDENTIFIER.match(path):
                failures.append((identifier, recorded["url"]))
            if path.casefold() in RESERVED_SEGMENTS:
                failures.append((identifier, f"reserved endpoint {recorded['url']}"))
    assert not failures, report(failures, len(HOSTILE_IDENTIFIERS))
    assert refused == len(HOSTILE_IDENTIFIERS), f"only {refused} refused; the rest were built"


def test_a_hostile_identifier_returned_by_the_listing_is_refused(opener):
    """Identifiers arrive from Gmail as well as from the caller; both are untrusted."""
    failures, refused = [], 0
    for identifier in HOSTILE_IDENTIFIERS:
        transport_pages = [{"messages": [{"id": identifier}]}]
        reader = GmailReader(
            credentials(),
            transport=lambda url, headers, p=iter(transport_pages): (
                200,
                json.dumps(next(p, {})).encode(),
            ),
        )
        try:
            found = reader.identifiers()
        except GmailError:
            refused += 1
            continue
        failures.append((identifier, found))
    assert not failures, report(failures, len(HOSTILE_IDENTIFIERS))
    assert refused == len(HOSTILE_IDENTIFIERS), f"only {refused} refused"


# The identifier guard fires first, so a family driven only through fetch can never reach
# the URL guard behind it. Stating the same invariant over URLs is what makes the second
# layer testable at all -- the mutation pass found this hole by removing that guard and
# watching every test still pass.
FOREIGN_PREFIXES = (
    "https://evil.example.com/gmail/v1/users/me/messages/",
    f"https://{GMAIL_HOST}.evil.example.com/gmail/v1/users/me/messages/",
    f"http://{GMAIL_HOST}/gmail/v1/users/me/messages/",
    f"https://{GMAIL_HOST}:8443/gmail/v1/users/me/messages/",
    f"https://{GMAIL_HOST}/upload/gmail/v1/users/me/messages/",
    f"https://{GMAIL_HOST}/gmail/v1/users/me/drafts/",
    f"https://{GMAIL_HOST}/gmail/v1/users/me/settings/",
    f"https://{GMAIL_HOST}/gmail/v2/users/me/messages/",
    f"https://{GMAIL_HOST}/gmail/v1/users/other/messages/",
)
URL_CASES = [GET_PREFIX + segment for segment in RESERVED_SPELLINGS] + [
    prefix + tail for prefix in FOREIGN_PREFIXES for tail in ("", "aaa1", "send", "aaa1?format=raw")
]


def test_no_generated_url_outside_the_allowlist_survives_the_guard():
    """A URL is the last thing seen before a bearer token is attached to it."""
    failures, refused = [], 0
    for url in URL_CASES:
        try:
            gmail.readable_url(url)
        except GmailError:
            refused += 1
            continue
        failures.append(url)
    assert not failures, report(failures, len(URL_CASES))
    assert refused == len(URL_CASES), f"only {refused} of {len(URL_CASES)} refused"


def test_the_two_allowlisted_reads_still_survive_the_guard():
    """The guard must refuse the family above without refusing the reads the adapter needs."""
    allowed = [LIST_URL, f"{LIST_URL}?q=label%3Ajobs&maxResults=10"] + [
        f"{GET_PREFIX}{identifier}?format=raw" for identifier in ("aaa1", "1111aaaa2222bbbb", "x")
    ]
    assert [gmail.readable_url(url) for url in allowed] == allowed


# A response that answers with a different identifier attaches one message's provenance to
# another. No family driven from well-behaved payloads can reach that check.
ECHO_CASES = [
    (requested, returned)
    for requested in ("aaa1", "bbb2")
    for returned in ("ccc3", "aaa1x", "", None, 17, "AAA1")
    if returned != requested
]


def test_a_response_naming_a_different_message_is_always_refused():
    failures = []
    for requested, returned in ECHO_CASES:
        reader = GmailReader(
            credentials(),
            transport=lambda url, headers, r=returned: (
                200,
                json.dumps({"id": r, "raw": encoded(BODY)}).encode(),
            ),
        )
        try:
            reader.fetch(requested)
        except GmailError:
            continue
        failures.append((requested, returned))
    assert not failures, report(failures, len(ECHO_CASES))


# --- Invariants 7 and 8: provenance and mailbox identity ---------------------------------


def message_for(identifier, header_id="<alert@example.com>", subject="Job alert", mailbox=MAILBOX):
    raw = BODY.replace(b"<alert@example.com>", header_id.encode()).replace(
        b"Subject: Job alert", f"Subject: {subject}".encode()
    )
    reader = GmailReader(
        credentials(mailbox=mailbox),
        transport=lambda url, headers: (
            200,
            json.dumps({"id": identifier, "raw": encoded(raw)}).encode(),
        ),
    )
    return reader.fetch(identifier)


PROVENANCE_CASES = [
    (first, second, header)
    for first, second in (("aaa1", "bbb2"), ("aaa1", "aaa2"), ("m1", "m2"))
    for header in ("<same@example.com>", "<>", "<a@example.com>")
]


def test_the_provider_identifier_decides_identity_not_the_rfc822_header():
    failures = []
    for first, second, header in PROVENANCE_CASES:
        left = message_for(first, header_id=header)
        right = message_for(second, header_id=header)
        if left.key == right.key or left.external_id != first or right.external_id != second:
            failures.append((first, second, header))
    assert not failures, report(failures, len(PROVENANCE_CASES))


def test_one_provider_identifier_keeps_one_identity_whatever_the_header_says():
    headers = ("<a@example.com>", "<b@example.com>", "<>", "<same@example.com>")
    keys = {message_for("aaa1", header_id=header).key for header in headers}
    assert len(keys) == 1, keys


def test_casing_alone_never_changes_mailbox_identity():
    failures = [
        (canonical, variant)
        for canonical, variant in MAILBOX_SPELLINGS
        if message_for("aaa1", mailbox=canonical).key != message_for("aaa1", mailbox=variant).key
    ]
    assert not failures, report(failures, len(MAILBOX_SPELLINGS))


def test_a_different_mailbox_always_changes_identity():
    failures = [
        (left, right)
        for left, right in DISTINCT_MAILBOXES
        if message_for("aaa1", mailbox=left).key == message_for("aaa1", mailbox=right).key
    ]
    assert not failures, report(failures, len(DISTINCT_MAILBOXES))


# --- Invariant 10: the families are not silently empty -----------------------------------


def test_the_generated_families_are_large_enough_to_be_worth_running():
    """An empty family makes every invariant above it pass while testing nothing."""
    sizes = {
        "unsafe characters": len(UNSAFE_CHARACTERS),
        "unsafe tokens": len(UNSAFE_TOKENS),
        "hostile identifiers": len(HOSTILE_IDENTIFIERS),
        "reserved spellings": len(RESERVED_SPELLINGS),
        "pagination": len(PAGINATION_CASES),
        "provenance": len(PROVENANCE_CASES),
        "mailbox spellings": len(MAILBOX_SPELLINGS),
        "distinct mailboxes": len(DISTINCT_MAILBOXES),
        "urls": len(URL_CASES),
        "echoed identities": len(ECHO_CASES),
    }
    assert all(count > 3 for count in sizes.values()), sizes
    assert sum(sizes.values()) > 300, sizes
    # The character family must actually span the control range, not a chosen few.
    assert set(string.whitespace) <= set(UNSAFE_CHARACTERS) | {" "}
    assert all(segment in RESERVED_SPELLINGS for segment in RESERVED_SEGMENTS)
    # The leak check compares against one stable string, so every generated token must
    # actually carry it. Without this, a placement that dropped the canary would make its
    # cases assert nothing -- which is exactly how the earlier TOKEN comparison went wrong.
    without = [token for token in UNSAFE_TOKENS if CANARY not in token]
    assert not without, f"{len(without)} generated tokens do not carry the canary"
