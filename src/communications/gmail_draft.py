"""Draft-only Gmail composition. This module defines no send operation.

The Gmail compose scope is not a draft-only grant: Google's own definition of
gmail.compose permits creating, updating and *sending* mail. The guarantee that
CareerSignal cannot send therefore cannot come from the scope string, and does not come
from it. It comes from this module:

  * no method here sends, and none constructs a send URL;
  * DRAFT_PATH admits only the drafts collection, so every other endpoint is refused
    before a bearer token is attached;
  * FORBIDDEN_SEGMENTS refuses the send, trash and modify spellings a final path segment
    could otherwise take -- including drafts/send, which DRAFT_PATH alone would admit.

The read adapter in communications.gmail is untouched by this module and keeps refusing
every scope but gmail.readonly. Two credentials, two adapters: a fault here cannot turn
the mailbox reader into a write path, which is the point of keeping them apart.

One read-only exception exists: identity() proves whose mailbox the compose credential
belongs to with a GET against users/me/profile, allowlisted by its own profile_url() and
never by writable_url(), which keeps refusing that path. It creates nothing.
"""

import base64
import json
import re
from dataclasses import dataclass
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as DEFAULT_POLICY
from email.utils import getaddresses
from urllib import error, request
from urllib.parse import quote, urlencode, urlsplit

from communications.gmail import GMAIL_HOST, HEADER_SAFE, GmailError
from communications.message import MAX_MESSAGE_BYTES
from recruiting.ports import DraftRefused as PortDraftRefused

API_ROOT = f"https://{GMAIL_HOST}/gmail/v1/"
COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
# A second variable, not a second use of the first. The read token and the compose token
# are different grants and are meant to be revocable independently.
COMPOSE_TOKEN_VARIABLE = "CAREERSIGNAL_GMAIL_COMPOSE_TOKEN"
MAX_RESPONSE_BYTES = 4 * MAX_MESSAGE_BYTES
NETWORK_TIMEOUT = 30.0

# The drafts collection and one draft within it. Nothing else in the Gmail API is spellable
# from this adapter, and the host is fixed rather than configurable.
DRAFT_PATH = re.compile(r"^/gmail/v1/users/me/drafts(?:/([0-9A-Za-z_-]{1,128}))?$")
IDENTIFIER = re.compile(r"^[0-9A-Za-z_-]{1,128}$")
# Segments that name an operation rather than a draft. "send" is the one that matters:
# users/me/drafts/send is the Gmail send endpoint and matches DRAFT_PATH's shape exactly.
FORBIDDEN_SEGMENTS = frozenset(
    {
        "send",
        "trash",
        "untrash",
        "modify",
        "import",
        "insert",
        "batchdelete",
        "batchmodify",
        "messages",
        "labels",
        "threads",
        "settings",
        "history",
        "profile",
        "watch",
        "stop",
        "attachments",
        "drafts",
    }
)

# Anything that cannot legally sit in a header value. Refused, never stripped: silently
# removing a character would address the message to somebody the operator did not read.
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
# A deliberately conservative address. Not RFC 5322 in full: this is what CareerSignal is
# willing to address a draft to, which is a smaller set than what the RFC permits.
ADDRESS = re.compile(
    r"^[0-9A-Za-z!#$%&'*+/=?^_`{|}~.-]{1,64}@[0-9A-Za-z.-]{1,190}\.[A-Za-z]{2,24}$"
)
MAX_SUBJECT = 400
# CareerSignal's own intent key, carried on the draft so a lost response can be reconciled
# by reading rather than by guessing which draft was probably ours.
INTENT_HEADER = "X-CareerSignal-Intent"
INTENT_KEY = re.compile(r"^[0-9A-Za-z_:.-]{1,128}$")
# Reconciliation reads a bounded window of the mailbox's drafts. Beyond this the answer is
# "unknown" rather than a wrong receipt.
MAX_DRAFTS = 100
MAX_DRAFT_PAGES = 10


def namespace_for(mailbox: str) -> str:
    """The stable destination name one mailbox is known by, normalized the same way once.

    A pure function rather than a method, so an operator's declared --mailbox and a
    verified emailAddress from Gmail's own profile response are turned into the same shape
    by the same code and can be compared for equality rather than merely similarity.
    """
    if not isinstance(mailbox, str):
        raise TypeError("A mailbox identifier is required")
    normalized = " ".join(mailbox.split()).casefold()
    if not normalized:
        raise ValueError("A mailbox identifier is required")
    return "gmail:" + normalized


class DraftRefused(PortDraftRefused, GmailError):
    """This invocation wrote no draft and reserved no new durable intent.

    Raised from a fresh draft attempt, the offending value is retained: refusing is not
    discarding, and the evidence that caused the refusal is untouched in storage, so the
    operator can correct the source and reevaluate. Raised while reconciling an
    already-unsettled intent, that intent is untouched instead -- this says what the
    reconciliation attempt did, not that nothing was ever reserved. It also carries the
    port's refusal type, so a caller can tell a refusal apart from a failure that may have
    happened after Gmail was contacted -- the two call for opposite handling.

    This can be raised after a read either way: verifying whose mailbox a compose
    credential belongs to is a GET against Gmail's profile endpoint, made solely to prove
    identity before anything is claimed or looked up. A profile read is not a draft write,
    so a refusal reached after one is exactly as certain as one reached without it --
    nothing was created and nothing was sent.
    """


@dataclass(frozen=True, repr=False)
class GmailComposeCredentials:
    """An authorized compose grant for one mailbox. Separate from the read grant."""

    access_token: str
    mailbox: str
    scopes: tuple[str, ...] = (COMPOSE_SCOPE,)

    def __post_init__(self):
        if not isinstance(self.access_token, str) or not self.access_token:
            raise ValueError("An access token is required")
        if not HEADER_SAFE.fullmatch(self.access_token):
            raise ValueError(
                "Access token must be visible ASCII with no spaces or control characters; "
                "check for a stray newline. The supplied value is not repeated here."
            )
        if not isinstance(self.mailbox, str):
            raise TypeError("A mailbox identifier is required")
        mailbox = " ".join(self.mailbox.split()).casefold()
        if not mailbox:
            raise ValueError("A mailbox identifier is required")
        object.__setattr__(self, "mailbox", mailbox)
        object.__setattr__(self, "scopes", tuple(self.scopes))
        # Refused rather than narrowed, as with the read grant. Configuring the readonly
        # scope here would be a mistake in the other direction and is refused too.
        if self.scopes != (COMPOSE_SCOPE,):
            raise ValueError(f"Only {COMPOSE_SCOPE} may be configured for this adapter")

    def __repr__(self):
        return f"GmailComposeCredentials(access_token=<redacted>, mailbox={self.mailbox!r})"

    @property
    def namespace(self) -> str:
        return namespace_for(self.mailbox)


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Following this would re-send the bearer token, and on a POST it would re-send the
        # draft body with it, to whatever host the response named.
        raise GmailError(f"Refused an HTTP {code} redirect; the request carries a bearer token")


_OPENER = request.build_opener(_NoRedirect)


def _perform(prepared) -> tuple[int, bytes]:
    try:
        # timeout by keyword: the opener's second positional parameter is the request body.
        with _OPENER.open(prepared, timeout=NETWORK_TIMEOUT) as response:
            body, status = response.read(MAX_RESPONSE_BYTES + 1), response.status
    except error.HTTPError as exc:
        return exc.code, exc.read(MAX_RESPONSE_BYTES + 1)
    except OSError as exc:
        raise GmailError(f"Gmail draft request failed: {type(exc).__name__}") from exc
    except ValueError:
        # The standard library rejects an illegal header by quoting the whole value, which
        # is where the bearer token lives. The cause is dropped rather than chained.
        raise GmailError("Gmail request headers were rejected before any network I/O") from None
    if len(body) > MAX_RESPONSE_BYTES:
        raise GmailError("Gmail response exceeds the read limit")
    return status, body


def https_create(url: str, headers: dict, body: bytes) -> tuple[int, bytes]:
    """Create a draft. The method is fixed here and is never a caller's parameter."""
    return _perform(request.Request(url, data=body, headers=headers, method="POST"))


def https_read(url: str, headers: dict) -> tuple[int, bytes]:
    """Read drafts back, for reconciliation only."""
    return _perform(request.Request(url, headers=headers, method="GET"))


def _validated_host(url: str):
    """Scheme, credential and host hygiene shared by every URL this adapter can request.

    Neither allowlist below is complete on its own: each still checks its own path against
    its own pattern. This only closes the parts a hostile URL could get wrong before either
    allowlist looks at a path at all.
    """
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
    return parts


def writable_url(url: str) -> str:
    """Refuse anything but an allowlisted Gmail draft URL, before a token is attached."""
    parts = _validated_host(url)
    match = DRAFT_PATH.match(parts.path)
    if not match:
        raise GmailError("Refused a path outside the Gmail draft allowlist")
    if match[1] and match[1].casefold() in FORBIDDEN_SEGMENTS:
        raise GmailError("Refused a reserved Gmail endpoint")
    return url


# The exact identity-read endpoint, and nothing shaped like it. Kept apart from DRAFT_PATH
# rather than folded into it: the drafts allowlist exists to keep this adapter from
# reaching anything but the drafts collection, and admitting /profile there would widen a
# boundary built for a different purpose to cover one it was never meant to.
PROFILE_PATH = "/gmail/v1/users/me/profile"


def profile_url(url: str) -> str:
    """Refuse anything but the one read used to prove whose mailbox a credential is."""
    parts = _validated_host(url)
    if parts.path != PROFILE_PATH or parts.query or parts.fragment:
        raise GmailError("Refused a path outside the Gmail profile identity endpoint")
    return url


def recipient(value) -> str:
    """The single address a draft may be addressed to, or a refusal.

    The value reaches here from a recruiter's From header, so it is hostile input. It is
    refused rather than repaired, and the refusal never echoes it: a message quoting
    "jane@example.com\\r\\nBcc: ..." puts the attempted injection into a log line.

    None of the checks below rely on the standard library failing closed, because it does
    not fail closed everywhere this program is supported. On CPython 3.11.9 --
    the interpreter hosted CI runs, and inside the supported range -- parseaddr returns the
    *first* address for a list rather than refusing it, so "a@example.com, b@example.com"
    parses as "a@example.com". Taking that would be sanitizing a hostile value into an
    accepted one, silently addressing a message the operator never read. The hardening that
    makes newer interpreters refuse it arrived after 3.11.9, so it cannot be depended on.
    """
    if not isinstance(value, str) or not value.strip():
        raise DraftRefused("Draft recipient is missing")
    # Scanned before parsing. parseaddr and getaddresses both pass a NUL straight through
    # into the address they return, on every supported version, so parsing is not the check.
    if CONTROL.search(value):
        raise DraftRefused(
            "Recipient contains prohibited control characters and was refused; "
            "the supplied value is not repeated here"
        )
    # getaddresses counts mailboxes rather than returning the first one, on every supported
    # version, and it still reads a quoted comma inside a display name as one address.
    parsed = getaddresses([value])
    if len(parsed) != 1:
        raise DraftRefused("Recipient names more than one mailbox and was refused")
    address = parsed[0][1]
    if not address or not ADDRESS.fullmatch(address):
        raise DraftRefused("Recipient is not a single usable email address and was refused")
    # The display name is dropped. It is recruiter-controlled text with no bearing on where
    # the message goes, and carrying it would mean encoding hostile text into a header.
    return address


def subject(value, *, prefix="Re: ") -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise DraftRefused("Draft subject is unusable")
    if CONTROL.search(value):
        raise DraftRefused(
            "Subject contains prohibited control characters and was refused; "
            "the supplied value is not repeated here"
        )
    text = " ".join(value.split())
    if len(text) > MAX_SUBJECT:
        raise DraftRefused("Subject exceeds the length CareerSignal will compose")
    if not text:
        return prefix.strip()
    return text if text.casefold().startswith(prefix.casefold()) else prefix + text


def intent_key(value) -> str:
    if not isinstance(value, str) or not INTENT_KEY.fullmatch(value):
        raise DraftRefused("Draft intent key is unusable")
    return value


def compose(*, to: str, subject_line: str, body: str, intent: str) -> bytes:
    """Build the RFC822 draft, then verify the result rather than trusting the builder."""
    if not isinstance(body, str) or not body.strip():
        raise DraftRefused("Draft body is empty")
    if len(body.encode()) > MAX_MESSAGE_BYTES:
        raise DraftRefused("Draft body exceeds the composition size limit")
    # Validated here as well as by the caller. Both checks are idempotent on a value that
    # already passed, and composition is the last place the headers can still be refused --
    # after this they are bytes on their way to somebody's mailbox. A boundary that is only
    # safe because its one current caller sanitizes first is not a boundary.
    to, subject_line = recipient(to), subject(subject_line)
    message = EmailMessage()
    try:
        message["To"] = to
        message["Subject"] = subject_line
        message[INTENT_HEADER] = intent
    except ValueError:
        # Second line only. recipient() and subject() have already refused these; the cause
        # is dropped because the standard library's message quotes the whole header value.
        raise DraftRefused("Header values were rejected during composition") from None
    message.set_content(body)
    raw = message.as_bytes()
    _verify(raw, to, intent)
    return raw


def header_value(value) -> str:
    """One header value with folding undone.

    A header longer than the line limit is folded onto a continuation line, and parsing it
    back returns the leading whitespace that folding inserted. A 64-character intent key
    folds every time, so comparing the raw parsed value against what was set would never
    match -- here, or against what Gmail returns during reconciliation.
    """
    return " ".join(value.split()) if isinstance(value, str) else ""


def _verify(raw: bytes, to: str, intent: str) -> None:
    """Read the built message back and prove it says what it was meant to say.

    Composition is the point where an injected header would appear, so the check is made
    against the serialized bytes rather than against the object that produced them.
    """
    built = BytesParser(policy=DEFAULT_POLICY).parsebytes(raw)
    if built.defects:
        raise DraftRefused("Composed draft did not parse cleanly and was refused")
    if [header_value(v) for v in built.get_all("To") or []] != [to]:
        raise DraftRefused("Composed draft does not address exactly the intended recipient")
    for header in ("Cc", "Bcc", "Reply-To", "Return-Path", "Sender"):
        if built.get_all(header):
            raise DraftRefused(f"Composed draft unexpectedly carries a {header} header")
    if len(built.get_all("Subject") or []) != 1:
        raise DraftRefused("Composed draft does not carry exactly one subject")
    if [header_value(v) for v in built.get_all(INTENT_HEADER) or []] != [intent]:
        raise DraftRefused("Composed draft does not carry exactly one intent key")


def _failure(status: int) -> str:
    if status == 401:
        return "Gmail rejected the access token (401); reauthorize with the compose scope"
    if status == 403:
        return "Gmail refused the draft (403); the grant may not cover this mailbox"
    if status == 429:
        return "Gmail rate limited the draft (429); retry later"
    return f"Gmail draft request failed with HTTP {status}"


def _identity_failure(status: int) -> str:
    if status == 401:
        return (
            "Gmail rejected the access token (401) while verifying mailbox identity; "
            "reauthorize with the compose scope"
        )
    if status == 403:
        return "Gmail refused the profile read (403) while verifying mailbox identity"
    if status == 429:
        return "Gmail rate limited the profile read (429); retry later"
    return f"Gmail profile read failed with HTTP {status}"


class GmailDrafts:
    """Creates Gmail drafts. It cannot send, delete, label or modify anything.

    Implements recruiting.ports.DraftProvider. The port defines create and lookup and no
    send operation, so nothing in the recruiting core can express one either.
    """

    # Fixed for every instance: which provider a request declares is a property of the
    # implementation, not of any one mailbox it happens to be constructed for.
    provider = "gmail"

    def __init__(
        self, credentials: GmailComposeCredentials, *, create=https_create, read=https_read
    ):
        if not isinstance(credentials, GmailComposeCredentials):
            raise TypeError("GmailComposeCredentials are required")
        self._credentials = credentials
        self._create, self._read = create, read

    @property
    def namespace(self) -> str:
        """The mailbox this credential was configured for. Declared, not yet verified.

        Approving and requesting a draft compare against this value, because both are the
        operator naming a destination before anything is contacted. Whether the credential
        actually belongs to that mailbox is a separate question, answered only by
        identity().
        """
        return self._credentials.namespace

    def _headers(self, **extra) -> dict:
        return {
            "Authorization": "Bearer " + self._credentials.access_token,
            "Accept": "application/json",
            **extra,
        }

    @staticmethod
    def _payload(status: int, body: bytes, *, failure=_failure) -> dict:
        if status not in (200, 201):
            raise GmailError(failure(status))
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GmailError("Gmail returned a body that is not JSON") from exc
        if not isinstance(value, dict):
            raise GmailError("Gmail returned an unexpected payload")
        return value

    @staticmethod
    def _draft_id(payload: dict) -> str:
        value = payload.get("id")
        if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
            raise GmailError("Gmail returned an unusable draft identifier")
        return value

    def identity(self) -> str:
        """Prove whose mailbox this credential belongs to, with one read-only request.

        `--mailbox` is an operator's typed expectation, not evidence. Google documents
        `users.me/profile` as readable under the same gmail.compose grant this adapter
        already holds -- no additional scope -- so one GET turns that expectation into a
        fact before anything is claimed or written. It is the only request this adapter
        makes that neither reads nor creates a draft, and it reserves nothing.
        """
        url = profile_url(API_ROOT + "users/me/profile")
        payload = self._payload(*self._read(url, self._headers()), failure=_identity_failure)
        address = payload.get("emailAddress")
        if not isinstance(address, str) or not address.strip():
            raise GmailError("Gmail profile response did not include an email address")
        return namespace_for(address)

    def refusal(self, key: str, body: str, *, to: str = "", subject_line: str = "") -> str | None:
        """Why this draft cannot be created, or None. Contacts nothing.

        This runs the real composition rather than a parallel set of checks. A second
        implementation of the same rules is a second source of truth, and the two would
        drift the first time only one of them was corrected.
        """
        try:
            compose(
                to=recipient(to),
                subject_line=subject(subject_line),
                body=body,
                intent=intent_key(key),
            )
        except DraftRefused as exc:
            return str(exc)
        return None

    def create(self, key: str, body: str, *, to: str = "", subject_line: str = "") -> str:
        """Create one draft and return its provider receipt. Never sends it."""
        raw = compose(
            to=recipient(to),
            subject_line=subject(subject_line),
            body=body,
            intent=intent_key(key),
        )
        request_body = json.dumps(
            {"message": {"raw": base64.urlsafe_b64encode(raw).decode("ascii")}}
        ).encode()
        url = writable_url(API_ROOT + "users/me/drafts")
        status, response = self._create(
            url, self._headers(**{"Content-Type": "application/json"}), request_body
        )
        return "gmail-draft:" + self._draft_id(self._payload(status, response))

    def _metadata_intent(self, draft_id: str) -> str | None:
        """Read one draft's intent header. Metadata only: no body is fetched."""
        url = writable_url(
            API_ROOT
            + "users/me/drafts/"
            + quote(draft_id, safe="")
            + "?"
            + urlencode([("format", "metadata"), ("metadataHeaders", INTENT_HEADER)])
        )
        payload = self._payload(*self._read(url, self._headers()))
        if payload.get("id") != draft_id:
            raise GmailError("Gmail returned a different draft than the one requested")
        message = payload.get("message")
        headers = (message or {}).get("payload", {}).get("headers")
        if not isinstance(message, dict) or not isinstance(headers, list):
            return None
        found = [
            header_value(entry.get("value"))
            for entry in headers
            if isinstance(entry, dict)
            and isinstance(entry.get("name"), str)
            and entry["name"].casefold() == INTENT_HEADER.casefold()
        ]
        # A draft carrying two intent headers is not evidence of anything. Say unknown.
        return found[0] if len(found) == 1 and found[0] else None

    def lookup(self, key: str) -> str | None:
        """Find a draft this program already created, without repeating the write.

        A draft carries CareerSignal's own intent key as a header, so reconciliation is a
        read and a comparison rather than a guess about which draft was probably ours. If
        the window is exhausted without a match, or two drafts claim the same intent, the
        answer is None -- unknown, not absent -- and the attempt stays uncertain.
        """
        wanted, token, pages, matches = intent_key(key), None, 0, []
        while pages < MAX_DRAFT_PAGES:
            parameters = [("maxResults", MAX_DRAFTS)]
            if token:
                parameters.append(("pageToken", token))
            url = writable_url(API_ROOT + "users/me/drafts?" + urlencode(parameters))
            page = self._payload(*self._read(url, self._headers()))
            entries = page.get("drafts") or []
            if not isinstance(entries, list):
                raise GmailError("Gmail returned an unexpected draft listing")
            for entry in entries:
                if not isinstance(entry, dict):
                    raise GmailError("Gmail returned an unexpected draft listing")
                draft_id = self._draft_id(entry)
                if self._metadata_intent(draft_id) == wanted:
                    matches.append(draft_id)
            token, pages = page.get("nextPageToken"), pages + 1
            if not token:
                break
        return "gmail-draft:" + matches[0] if len(matches) == 1 else None
