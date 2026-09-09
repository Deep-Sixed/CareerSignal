"""Read-only Gmail intake. This module defines no send, draft or modify operation."""

import base64
import binascii
import json
import re
from dataclasses import dataclass
from urllib import error, request
from urllib.parse import quote, urlencode, urlsplit

from communications.message import MAX_MESSAGE_BYTES, Message

GMAIL_HOST = "gmail.googleapis.com"
API_ROOT = f"https://{GMAIL_HOST}/gmail/v1/"
READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
# The access token is read from the environment so it never reaches a command line, a shell
# history file, or another user's view of the process table.
TOKEN_VARIABLE = "CAREERSIGNAL_GMAIL_TOKEN"
# A response is held in memory before it is parsed, so it is bounded on its own: a maximum
# message expands by a third in base64 and travels inside a JSON envelope.
MAX_RESPONSE_BYTES = 4 * MAX_MESSAGE_BYTES
MAX_RESULTS = 100
MAX_PAGES = 50
NETWORK_TIMEOUT = 30.0

# Only these two paths exist for this adapter. The host is fixed rather than configurable:
# a bearer token must never be offered to a host the operator did not authorize.
READ_PATH = re.compile(r"^/gmail/v1/users/me/messages(?:/([0-9A-Za-z_-]{1,128}))?$")
IDENTIFIER = re.compile(r"^[0-9A-Za-z_-]{1,128}$")
# Segments that name a Gmail operation or collection rather than a message. A malformed or
# hostile identifier must not be spellable into one of them.
RESERVED_SEGMENTS = frozenset(
    {
        "send",
        "import",
        "insert",
        "batchdelete",
        "batchmodify",
        "trash",
        "untrash",
        "modify",
        "drafts",
        "settings",
        "watch",
        "stop",
        "attachments",
        "labels",
        "threads",
        "history",
        "profile",
        "messages",
    }
)


class GmailError(RuntimeError):
    """A Gmail read could not be completed. Never carries the access token."""


@dataclass(frozen=True, repr=False)
class GmailCredentials:
    """An authorized read grant for one mailbox."""

    access_token: str
    mailbox: str
    scopes: tuple[str, ...] = (READONLY_SCOPE,)

    def __post_init__(self):
        if not isinstance(self.access_token, str) or not self.access_token.strip():
            raise ValueError("An access token is required")
        if not isinstance(self.mailbox, str):
            raise TypeError("A mailbox identifier is required")
        # Gmail addresses are not case sensitive, so two spellings of one mailbox must not
        # split the same message into two identities.
        mailbox = " ".join(self.mailbox.split()).casefold()
        if not mailbox:
            raise ValueError("A mailbox identifier is required")
        object.__setattr__(self, "mailbox", mailbox)
        object.__setattr__(self, "scopes", tuple(self.scopes))
        # A broader grant is refused rather than quietly narrowed: this program has no write
        # path, so anything beyond the read scope can only be a configuration mistake. This
        # constrains what CareerSignal asks for; it does not constrain the token it is given.
        if self.scopes != (READONLY_SCOPE,):
            raise ValueError(f"Only {READONLY_SCOPE} may be configured for this adapter")

    def __repr__(self):
        # A token must not reach a log line, a traceback frame dump or a test report.
        return f"GmailCredentials(access_token=<redacted>, mailbox={self.mailbox!r})"

    @property
    def namespace(self) -> str:
        return "gmail:" + self.mailbox


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Following this would re-send the bearer token to whatever host the response named.
        raise GmailError(f"Refused an HTTP {code} redirect; the request carries a bearer token")


_OPENER = request.build_opener(_NoRedirect)


def https_get(url: str, headers: dict) -> tuple[int, bytes]:
    """The only network call in CareerSignal. The method is fixed, not a parameter."""
    # timeout is passed by keyword: the opener's second positional parameter is the request
    # body, and filling it would turn this read into a request that carries data.
    get = request.Request(url, headers=headers, method="GET")
    try:
        with _OPENER.open(get, timeout=NETWORK_TIMEOUT) as response:
            body, status = response.read(MAX_RESPONSE_BYTES + 1), response.status
    except error.HTTPError as exc:
        return exc.code, exc.read(MAX_RESPONSE_BYTES + 1)
    except OSError as exc:
        # The reason may quote the URL or the server; the token is never in either.
        raise GmailError(f"Gmail read failed: {type(exc).__name__}") from exc
    if len(body) > MAX_RESPONSE_BYTES:
        raise GmailError("Gmail response exceeds the read limit")
    return status, body


def readable_url(url: str) -> str:
    """Refuse anything but an allowlisted Gmail read URL, before a token is attached."""
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise GmailError("Refused a Gmail URL that is not HTTPS")
    if parts.username or parts.password:
        raise GmailError("Refused a Gmail URL carrying credentials")
    try:
        port = parts.port
    except ValueError as exc:
        raise GmailError("Refused a Gmail URL with an unreadable port") from exc
    if parts.hostname != GMAIL_HOST or port not in (None, 443):
        raise GmailError(f"Refused a request to a host other than {GMAIL_HOST}")
    match = READ_PATH.match(parts.path)
    if not match:
        raise GmailError("Refused a path outside the Gmail read allowlist")
    # Checked here as well as on the identifier: the URL is the last thing seen before a
    # bearer token is attached to it, so it is where the guarantee has to hold.
    if match[1] and match[1].casefold() in RESERVED_SEGMENTS:
        raise GmailError("Refused a reserved Gmail endpoint")
    return url


def _url(path: str, parameters=()) -> str:
    query = urlencode(list(parameters), doseq=True)
    return readable_url(API_ROOT + path + (f"?{query}" if query else ""))


def _identifier(value) -> str:
    # The value is never echoed: a listing entry is content Gmail relayed from elsewhere.
    if not isinstance(value, str) or not IDENTIFIER.match(value):
        raise GmailError("Gmail message identifier is unusable")
    if value.casefold() in RESERVED_SEGMENTS:
        raise GmailError("Gmail message identifier is unusable: it names a reserved endpoint")
    return value


def _failure(status: int) -> str:
    if status == 401:
        return "Gmail rejected the access token (401); reauthorize with the read scope"
    if status == 403:
        return "Gmail refused the read (403); the grant may not cover this mailbox"
    if status == 429:
        return "Gmail rate limited the read (429); retry later"
    return f"Gmail read failed with HTTP {status}"


class GmailReader:
    """Reads authorized Gmail messages. It cannot create, send or alter anything."""

    def __init__(self, credentials: GmailCredentials, transport=https_get):
        if not isinstance(credentials, GmailCredentials):
            raise TypeError("GmailCredentials are required")
        self._credentials = credentials
        self._transport = transport

    @property
    def namespace(self) -> str:
        return self._credentials.namespace

    def _read(self, url: str) -> dict:
        status, body = self._transport(
            url,
            {
                "Authorization": "Bearer " + self._credentials.access_token,
                "Accept": "application/json",
            },
        )
        if status != 200:
            raise GmailError(_failure(status))
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GmailError("Gmail returned a body that is not JSON") from exc
        if not isinstance(value, dict):
            raise GmailError("Gmail returned an unexpected payload")
        return value

    def identifiers(self, *, query: str = "", label_ids=(), limit: int = MAX_RESULTS) -> tuple:
        if type(limit) is not int or limit < 1:
            raise ValueError("A positive result limit is required")
        if isinstance(label_ids, str):
            raise ValueError("Label identifiers must be a sequence, not a single string")
        # A mailbox is not a snapshot, so one identifier can appear on two pages. Unique
        # identifiers are counted toward the limit as they are seen, because letting a repeat
        # spend the budget would silently drop a distinct message that was still to come.
        found, token, pages = {}, None, 0
        while len(found) < limit and pages < MAX_PAGES:
            parameters = [("maxResults", min(limit - len(found), MAX_RESULTS))]
            if query:
                parameters.append(("q", query))
            parameters.extend(("labelIds", label) for label in label_ids)
            if token:
                parameters.append(("pageToken", token))
            page = self._read(_url("users/me/messages", parameters))
            entries = page.get("messages")
            if entries is None:
                entries = []
            if not isinstance(entries, list):
                raise GmailError("Gmail returned an unexpected message listing")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise GmailError("Gmail returned an unexpected message listing")
                found[_identifier(entry.get("id"))] = None
                if len(found) >= limit:
                    break
            token, pages = page.get("nextPageToken"), pages + 1
            if not token:
                break
        # dict preserves first-seen order, which is the order the mailbox offered them.
        return tuple(found)

    def fetch(self, message_id) -> Message:
        identifier = _identifier(message_id)
        path = "users/me/messages/" + quote(identifier, safe="")
        payload = self._read(_url(path, [("format", "raw")]))
        if payload.get("id") != identifier:
            raise GmailError("Gmail returned a different message than the one requested")
        raw = payload.get("raw")
        if not isinstance(raw, str) or not raw:
            raise GmailError("Gmail returned no raw message body")
        try:
            content = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
        except (binascii.Error, ValueError) as exc:
            raise GmailError("Gmail returned an undecodable message body") from exc
        # Provenance is the provider identifier, never the RFC822 Message-ID header: that
        # header is written by whoever sent the mail and two messages can carry the same one.
        return Message.from_bytes(content, namespace=self.namespace, provider_id=identifier)

    def messages(self, *, query: str = "", label_ids=(), limit: int = MAX_RESULTS) -> tuple:
        """Read whole messages. One unreadable message stops the batch rather than vanishing."""
        return tuple(
            self.fetch(identifier)
            for identifier in self.identifiers(query=query, label_ids=label_ids, limit=limit)
        )
