"""A loopback-only, token-authenticated HTTP projection of what is stored, plus its commands.

This is the first CareerSignal surface that listens on a socket, so it is defined by what
it refuses. It binds 127.0.0.1 and offers no way to bind anything else. It answers GET and
HEAD, and POST only at the addresses written out below, and refuses every other method
before routing.

One command appends a status event; the other records a decision. Everything else it
reaches is a read projection: no intake, no claim, no draft, no reconciliation, no provider
and no credential. What this package may change is declared once, in `docs/web-surface.md`,
and a test compares that declaration against this package's own syntax tree -- so widening it
means saying so there, in the same change, rather than discovering later that the code and
the documentation stopped agreeing.

An approval is recorded only while the packet it was read from still holds. The comparison
is `decide()`'s, inside its own write transaction; nothing here re-implements it, because a
comparison at this layer could only read outside that transaction. A rejection carries no
such expectation: it binds nothing and authorizes nothing, and putting the safest action an
operator can take behind a fresh packet would make it harder to stop something than to send
it. Where an approval points is a launch fact, two strings chosen before the socket was
bound, so no request can name a destination the operator did not choose.

Authority is a token generated once per launch and held in process memory. It is never
stored, never logged and never reported by the API. It travels to the browser in the URL
fragment, which browsers do not send in HTTP requests, and comes back as a request header.

Nothing here derives a fact. Every route hands back what a repository projection already
returned, because the storage rules were settled where the storage is; a second opinion
computed at the edge is how a list and a detail pane start disagreeing.
"""

import json
import os
import secrets
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, unquote, urlsplit

from communications.gmail import TOKEN_VARIABLE
from communications.gmail_draft import COMPOSE_TOKEN_VARIABLE
from data.store import sqlite_report
from recruiting.models import BindingConflict
from recruiting.status import STATUSES, StatusConflict
from system import views

# The only address this surface knows how to bind. There is no --host and no fallback: an
# interface is not a setting when the whole security model is "nothing off this machine".
LOOPBACK = "127.0.0.1"
DEFAULT_PORT = 8765
# Port 0 lets the operating system allocate a free port. Tests use it so two runs cannot
# collide; the bound port is read back from the socket rather than assumed either way.
API_ROOT = "/api/v1"
TOKEN_HEADER = "X-CareerSignal-Token"
TOKEN_BYTES = 32
JSON = "application/json; charset=utf-8"

# No inline script and no eval, so the frontend that arrives next can be served under this
# policy unchanged rather than loosening it on the way in.
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "base-uri 'none'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
        "connect-src 'self'",
        "img-src 'self' data:",
        "style-src 'self'",
        "script-src 'self'",
    )
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}
# An asset this surface cannot name a media type for is a packaging mistake, not something
# to guess at with a sniffed type behind X-Content-Type-Options: nosniff.
MEDIA_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
}
FILTERS = ("status", "active", "eligible", "min_coverage", "max_coverage")
WINDOW = ("limit", "since")
# Opt-in, because the eight reads established in #29 are the repository's projections
# exactly, and a caller that asked for one should keep getting one.
PRESENTATION = "presentation"
# The commands this surface answers, and the whole of what each accepts. Adding one means
# adding a case beside the reads and declaring it in docs/web-surface.md, which a test checks.
COMMAND = ("opportunities", "status")
COMMAND_FIELDS = frozenset({"status", "reason", "expected_event_id"})
DECISION = ("reviews", "decision")
DECISION_FIELDS = frozenset({"approved", "expected"})
# The four facts an approval binds, named here only to refuse a body that is not shaped
# like one. What they mean is decide()'s to judge, inside the transaction that depends on
# the answer.
EXPECTATION_FIELDS = frozenset(
    {"content_digest", "draft_digest", "addressing_digest", "status_event_id"}
)
# Three short fields and an operator's note. The ceiling exists so an oversized request is
# refused on what it declares rather than after it has been read into memory.
MAX_BODY = 16 * 1024
# Never taken from the request. The authenticated local browser is the operator, and an
# actor supplied by JavaScript would let presentation input rewrite audit identity.
OPERATOR = "operator"
# Where an approval says a draft may go. A launch fact, fixed for the process, never a
# field: a destination the browser could name is a destination a page could redirect.
DEFAULT_PROVIDER = "controlled"
# What this surface answers at all. A command path narrows further; everything else is
# still refused by default.
METHODS = "GET, HEAD, POST"
READ_METHODS = "GET, HEAD"


def assets() -> dict:
    """Every packaged static file, read once through importlib.resources.

    Keyed by its exact name. Nothing in this module joins a path or resolves one, so a
    request naming `../../etc/passwd` is not a traversal to defeat -- it is a key that does
    not exist. Read from package resources rather than from a directory beside __file__,
    so the installed wheel serves the same bytes with no checkout anywhere.
    """
    found = {}
    for entry in (resources.files("system.web") / "static").iterdir():
        if not entry.is_file():
            continue
        suffix = entry.name[entry.name.rfind(".") :].casefold()
        if suffix not in MEDIA_TYPES:
            raise RuntimeError(f"Packaged asset with no servable media type: {entry.name}")
        found[entry.name] = (entry.read_bytes(), MEDIA_TYPES[suffix])
    return found


def session(repository, target) -> dict:
    """What this machine is set up with. Presence booleans only, never a credential value.

    `decision_target` is where an approval recorded here would say a draft may go. It is a
    name, not a credential, and it is fixed for the process: approving declares a
    destination, and declaring one has never needed the ability to reach it.
    """
    provider, namespace = target
    return {
        **sqlite_report(),
        "database": str(repository.path),
        "gmail_token": bool(os.getenv(TOKEN_VARIABLE, "").strip()),
        "gmail_compose_token": bool(os.getenv(COMPOSE_TOKEN_VARIABLE, "").strip()),
        "decision_target": {"provider": provider, "provider_namespace": namespace},
    }


def tail(path):
    """The address beneath the API root, or None for a path that is not beneath it.

    The prefix is proven rather than assumed. Slicing by `len(API_ROOT)` alone strips any
    seven characters, so `/abcdef/opportunities/x/status` would present exactly the
    segments `/api/v1/opportunities/x/status` does, and an address outside the API would
    reach whatever that address routes to.

    Empty components are kept rather than filtered out, so a doubled or trailing slash is
    a different address than the one the contract names instead of an alias for it. Each
    component is unquoted after the split, so a `%2F` inside an identifier stays one
    component rather than dividing the address.
    """
    if path == API_ROOT:
        return []
    if not path.startswith(API_ROOT + "/"):
        return None
    return [unquote(part) for part in path[len(API_ROOT) + 1 :].split("/")]


def one(query, name) -> str:
    values = query[name]
    if len(values) != 1:
        raise ValueError(f"{name} may be given once")
    return values[0]


def boolean(query, name) -> bool:
    value = one(query, name).casefold()
    if value not in ("true", "false"):
        raise ValueError(f"{name} must be true or false")
    return value == "true"


def whole(query, name) -> int:
    value = one(query, name)
    if not (value.isascii() and value.isdigit()):
        raise ValueError(f"{name} must be a whole number")
    return int(value)


def accepted(query, names) -> None:
    """Refuse a parameter this route does not know.

    A misspelled filter that silently returned everything would tell the operator they are
    looking at a narrowed list when they are looking at all of it.
    """
    unknown = sorted(set(query) - set(names))
    if unknown:
        raise ValueError(f"Unknown parameter: {', '.join(unknown)}")


def filters(query) -> dict:
    """Query strings as the arguments opportunities() already validates for itself."""
    accepted(query, FILTERS + (PRESENTATION,))
    supplied = {}
    if "status" in query:
        supplied["status"] = one(query, "status")
    for name in ("active", "eligible"):
        if name in query:
            supplied[name] = boolean(query, name)
    for name in ("min_coverage", "max_coverage"):
        if name in query:
            supplied[name] = whole(query, name)
    return supplied


def window(query) -> dict:
    accepted(query, WINDOW)
    return {name: whole(query, name) for name in WINDOW if name in query}


class Oversized(ValueError):
    """A request declaring more bytes than this command could ever need."""


def command(payload) -> dict:
    """The three fields this command accepts, and nothing else.

    Every refusal here is about shape. What a value *means* -- whether the status is in the
    vocabulary, whether the event is still the newest one -- belongs to record_status(),
    which judges it inside the transaction that depends on the answer. Checking either here
    would be a second opinion that could disagree with the one that counts.
    """
    if not isinstance(payload, dict):
        raise ValueError("The request body must be a JSON object")
    unknown = sorted(set(payload) - COMMAND_FIELDS)
    if unknown:
        raise ValueError(f"Unknown field: {', '.join(unknown)}")
    for name in ("status", "expected_event_id"):
        if name not in payload:
            raise ValueError(f"{name} is required")
    if not isinstance(payload["status"], str):
        raise ValueError("status must be text")
    # `type(...) is not int` rather than isinstance, because bool is a subclass of int and
    # `true` is not an event id.
    if type(payload["expected_event_id"]) is not int:
        raise ValueError("expected_event_id must be a whole number")
    reason = payload.get("reason", "")
    if not isinstance(reason, str):
        raise ValueError("reason must be text")
    return {
        "status": payload["status"],
        "reason": reason,
        "expected_event_id": payload["expected_event_id"],
    }


def decision(payload) -> dict:
    """An approval carries the packet it was read from; a rejection carries nothing.

    The asymmetry is `decide()`'s, not this layer's invention. An approval authorizes an
    outward draft to a destination, so it is recorded only while the four facts the
    operator saw still hold. A rejection binds nothing and authorizes nothing, and putting
    the safest action an operator can take behind a fresh packet would make it harder to
    stop something than to send it.

    So `expected` is required with `approved: true` and refused with `approved: false` --
    refused rather than ignored, because a caller that sent one believed it was being
    honoured, and silently dropping it would answer a question nobody asked.
    """
    if not isinstance(payload, dict):
        raise ValueError("The request body must be a JSON object")
    unknown = sorted(set(payload) - DECISION_FIELDS)
    if unknown:
        raise ValueError(f"Unknown field: {', '.join(unknown)}")
    if "approved" not in payload:
        raise ValueError("approved is required")
    # `type(...) is not bool` rather than truthiness: 1, "yes" and a non-empty list are all
    # true, and none of them is a decision somebody made.
    if type(payload["approved"]) is not bool:
        raise ValueError("approved must be true or false")
    if not payload["approved"]:
        if "expected" in payload:
            raise ValueError("A rejection binds nothing and may not carry an expectation")
        return {"approved": False, "expected": None}
    if "expected" not in payload:
        raise ValueError("An approval must carry the expectation it was read from")
    return {"approved": True, "expected": expectation(payload["expected"])}


def expectation(supplied) -> dict:
    """The four facts, all of them, each the right kind of value and nothing else.

    Shape only. Whether these are still *true* is compared inside decide()'s transaction,
    where nothing can commit between the comparison and the row that depends on it.
    """
    if not isinstance(supplied, dict):
        raise ValueError("expected must be a JSON object")
    missing = sorted(EXPECTATION_FIELDS - set(supplied))
    if missing:
        raise ValueError(f"expected is missing: {', '.join(missing)}")
    unknown = sorted(set(supplied) - EXPECTATION_FIELDS)
    if unknown:
        raise ValueError(f"Unknown field in expected: {', '.join(unknown)}")
    for name in ("content_digest", "draft_digest", "addressing_digest"):
        if not isinstance(supplied[name], str):
            raise ValueError(f"{name} must be text")
    # bool is a subclass of int, and `true` is not an event id.
    if type(supplied["status_event_id"]) is not int:
        raise ValueError("status_event_id must be a whole number")
    return {name: supplied[name] for name in sorted(EXPECTATION_FIELDS)}


def presenting(query) -> bool:
    """Whether this request asked for the rendered strings as well as the stored facts."""
    return PRESENTATION in query and boolean(query, PRESENTATION)


def presented(summary, action) -> dict:
    """The four strings an operator reads, from the one implementation that owns them.

    Every value here is a call into `system.views` and nothing else. The alternative the
    frozen handoff proposed -- porting these four to JavaScript -- would put CareerSignal's
    idea of what its own state means into a second language, where `queue()`'s precedence
    could drift from the Python that is mutation-tested. A browser that renders a string it
    was handed cannot disagree with the engine about whether an approval still binds.
    """
    return {
        "coverage": views.coverage(summary),
        "queue": views.queue(summary, action),
        "approval": views.approval(action),
        "attempt": views.attempt(action),
    }


def enriched(repository, rows) -> list:
    """List rows with their presentation block.

    `opportunities()` reports no action state, so each row's is read from the detail the
    repository already assembles. That is one read per row, accepted deliberately for a
    local single-operator application: the alternative is a second summary model kept in
    step with `_action` by hand, which is the duplication this whole change exists to
    avoid. Optimise it when a real database is measurably slow, not before.
    """
    return [
        {**row, "presentation": presented(row, repository.opportunity(row["id"])["action"])}
        for row in rows
    ]


class Handler(BaseHTTPRequestHandler):
    """One request. Refusals are decided before anything is routed or read."""

    server_version = "CareerSignal"
    sys_version = ""

    def log_message(self, format, *args):
        """Write nothing.

        A request log is the one place a launch token could outlive the process that owns
        it. Nothing reaches the URL that is worth a file, and the operator's own terminal
        already shows what they started.
        """

    def __getattr__(self, name):
        """Every method but the three below, including verbs this has never heard of.

        Answering here rather than enumerating PUT, PATCH and DELETE means a request does
        not need to be anticipated to be refused: the refusal is the default, and reaching
        a route is what has to be spelled out. POST narrows that default only at the
        addresses written out below, rather than replacing it with a router that would
        accept another the day somebody registers one.
        """
        if name.startswith("do_"):
            return self._refuse_method
        raise AttributeError(name)

    def _refuse_method(self):
        self._send(
            HTTPStatus.METHOD_NOT_ALLOWED,
            JSON,
            json.dumps({"error": "This surface reads, and records a status"}).encode("utf-8"),
            body=True,
            extra={"Allow": METHODS},
        )

    def do_GET(self):
        self._send(*self._resolve(), body=True)

    def do_HEAD(self):
        self._send(*self._resolve(), body=False)

    def do_POST(self):
        self._send(*self._command(), body=True)

    # --- the boundary ---------------------------------------------------------------------

    def _resolve(self):
        parsed = urlsplit(self.path)
        if self.headers.get("Host") != self.server.authority:
            # A request naming any other host arrived here by having that name pointed at
            # loopback, which is the shape DNS rebinding takes. The socket is not the
            # identity; the name the client asked for is.
            return self._error(HTTPStatus.FORBIDDEN, "Unexpected Host")
        origin = self.headers.get("Origin")
        if origin is not None and origin != self.server.origin:
            return self._error(HTTPStatus.FORBIDDEN, "Unexpected Origin")
        if not self._origin_form(parsed):
            return self._error(HTTPStatus.BAD_REQUEST, "Unsupported request target")
        segments = tail(parsed.path)
        if segments is not None:
            return self._api(parsed, segments)
        return self._static(parsed.path)

    def _origin_form(self, parsed) -> bool:
        """A request target this surface answers: a path, carrying no scheme and no authority.

        A target in absolute form -- `http://127.0.0.1:8765/api/v1/...` -- names its own
        authority, and HTTP makes that authority, not the Host header, the one that
        identifies the server. This surface settles identity on Host, so honouring absolute
        form would mean routing by one authority while checking another, and would give the
        single command address a second spelling. It is refused rather than half-honoured.
        """
        return not parsed.scheme and not parsed.netloc

    def _authorized(self) -> bool:
        presented = self.headers.get(TOKEN_HEADER, "")
        # Compared as bytes so a header carrying anything but ASCII is a mismatch rather
        # than an exception, and in constant time either way.
        return secrets.compare_digest(
            presented.encode("utf-8", "surrogateescape"), self.server.token.encode("utf-8")
        )

    def _api(self, parsed, segments):
        if not self._authorized():
            return self._error(HTTPStatus.UNAUTHORIZED, "A valid launch token is required")
        try:
            # keep_blank_values, because the default drops `?status=` entirely and this
            # route's whole query contract is fail-closed: a parameter that disappears
            # before it is looked at is a narrowed request answered with an unnarrowed
            # list, which is the one wrong answer that looks right.
            payload = self._projection(segments, parse_qs(parsed.query, keep_blank_values=True))
        except KeyError:
            return self._error(HTTPStatus.NOT_FOUND, "No record with that id")
        except ValueError as exc:
            # The repository's own refusal, in its own words: this layer validates shapes
            # and leaves the meaning of a value to the projection that owns it.
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        if payload is None:
            return self._error(HTTPStatus.NOT_FOUND, "No such route")
        return (HTTPStatus.OK, JSON, json.dumps(payload).encode("utf-8"), None)

    def _projection(self, segments, query):
        """One thin adapter per existing read. No route computes anything of its own."""
        repository = self.server.repository
        match segments:
            case ["session"]:
                accepted(query, ())
                return session(repository, self.server.decision_target)
            case ["opportunities"]:
                rows = repository.opportunities(**filters(query))
                return enriched(repository, rows) if presenting(query) else rows
            case ["opportunities", identifier]:
                accepted(query, (PRESENTATION,))
                record = repository.opportunity(identifier)
                if not presenting(query):
                    return record
                return {**record, "presentation": presented(record, record["action"])}
            case ["opportunities", identifier, "sources"]:
                accepted(query, ())
                # Absence is decided first, exactly as the CLI decides it: an id naming
                # nothing is a 404, while an opportunity with no stored source is an
                # honest empty list. The two must not arrive looking the same.
                repository.opportunity(identifier)
                return repository.sources(identifier)
            case ["communications"]:
                accepted(query, ())
                return repository.communications()
            case ["communications", message_id]:
                accepted(query, ())
                return repository.communication(message_id)
            case ["reviews", review_id, "authorization"]:
                accepted(query, ())
                # Same rule: nonexistence is a 404 here, while a review that exists and is
                # stale keeps the repository's own refusal and its own words.
                repository.review(review_id)
                return repository.authorization(review_id)
            case ["timeline"]:
                return repository.timeline(**window(query))
            case ["statuses"]:
                accepted(query, ())
                # The vocabulary itself, so a <select> can be filled without the browser
                # holding a second copy that could fall out of step with the engine. The
                # order is the module's presentation order; it carries no rule, and
                # transitions are deliberately unrestricted.
                return {"statuses": list(STATUSES)}
        return None

    # --- the commands --------------------------------------------------------------------

    def _command(self):
        """Provenance before payload.

        Host, Origin and token are settled before a byte of the body is read, so a request
        that cannot prove where it came from never gets as far as being parsed. Origin is
        required here rather than merely checked when present: a read with no Origin is an
        ordinary same-document fetch, but a write with none has nothing to say for itself.
        """
        parsed = urlsplit(self.path)
        if self.headers.get("Host") != self.server.authority:
            return self._error(HTTPStatus.FORBIDDEN, "Unexpected Host")
        if self.headers.get("Origin") != self.server.origin:
            return self._error(
                HTTPStatus.FORBIDDEN, "A command must carry this surface's own Origin"
            )
        if not self._authorized():
            return self._error(HTTPStatus.UNAUTHORIZED, "A valid launch token is required")
        # After the provenance checks, so the ordering above is the ordering the contract
        # names: this is a question about the request line, not about who is asking.
        if not self._origin_form(parsed):
            return self._error(HTTPStatus.BAD_REQUEST, "Unsupported request target")
        match tail(parsed.path):
            case ["opportunities", identifier, "status"]:
                return self._record_status(identifier, parsed.query)
            case ["reviews", identifier, "decision"]:
                return self._decide(identifier, parsed.query)
        # Every other address reads -- including a path outside the API root, for which
        # `tail` returns None and no sequence pattern above can match. Saying so with Allow
        # rather than 404 keeps a POST from reporting which read routes exist.
        return (
            HTTPStatus.METHOD_NOT_ALLOWED,
            JSON,
            json.dumps({"error": "No command at this address"}).encode("utf-8"),
            {"Allow": READ_METHODS},
        )

    def _decide(self, identifier, query):
        """Record one decision, or refuse and record nothing.

        The comparison lives in `decide()`, which reads the binding inside the write
        transaction and raises there. Nothing here pre-checks it: a comparison at this
        layer could only read outside that transaction, and a new message could commit
        between the check and the row that depended on it -- which is the whole of what
        `BindingConflict` exists to stop.

        The provider and namespace come from the launch, never from the request. An
        approval names where a draft may go, and a destination a page could name is a
        destination a page could redirect.
        """
        repository = self.server.repository
        try:
            accepted(parse_qs(query, keep_blank_values=True), ())
            supplied = decision(self._body())
        except Oversized as refused:
            return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, str(refused))
        except ValueError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        try:
            # Absence first, so a review id naming nothing is a 404 rather than the
            # repository's refusal about a review that was never there.
            repository.review(identifier)
        except KeyError:
            return self._error(HTTPStatus.NOT_FOUND, "No record with that id")
        provider, namespace = self.server.decision_target
        try:
            repository.decide(
                identifier,
                approved=supplied["approved"],
                actor=OPERATOR,
                provider=provider,
                provider_namespace=namespace,
                expected=supplied["expected"],
            )
        except BindingConflict as conflict:
            # Caught by type, before the ValueError branch below can see it. The request
            # was well formed and the operator's authority was real; the packet moved.
            # Nothing was written -- no decision row, no audit event -- so an approval that
            # already stood stands exactly as it was.
            return (
                HTTPStatus.CONFLICT,
                JSON,
                json.dumps(
                    {
                        "error": "binding_conflict",
                        "expected": conflict.expected,
                        "observed": conflict.observed,
                    }
                ).encode("utf-8"),
                None,
            )
        except ValueError as exc:
            # A terminal opportunity, a below-threshold review, a decision locked behind a
            # draft intent: the request was well formed and the state refused it. That is a
            # conflict with what is stored, not a malformed body, and answering 400 would
            # tell an operator to fix a request that was never wrong.
            return (
                HTTPStatus.CONFLICT,
                JSON,
                json.dumps({"error": "decision_refused", "detail": str(exc)}).encode("utf-8"),
                None,
            )
        # `decide()` returns nothing: it either wrote the decision or raised. So this says
        # what was recorded rather than echoing a return value, and the destination appears
        # only on an approval, which is the only decision that authorizes one. Everything
        # else the browser needs it gets by reading again.
        recorded = {"review": identifier, "approved": supplied["approved"], "actor": OPERATOR}
        if supplied["approved"]:
            recorded |= {"provider": provider, "provider_namespace": namespace}
        return (HTTPStatus.OK, JSON, json.dumps(recorded).encode("utf-8"), None)

    def _body(self) -> dict:
        """The declared length, the ceiling, then exactly that many bytes, then JSON."""
        media = self.headers.get("Content-Type", "").split(";")[0].strip().casefold()
        if media != "application/json":
            raise ValueError("A command must be sent as application/json")
        declared = self.headers.get("Content-Length", "")
        if not (declared.isascii() and declared.isdigit()):
            # Without a length there is nothing to bound, and this surface does not decode
            # a chunked body: an unbounded read is exactly what the ceiling exists to stop.
            raise ValueError("Content-Length is required")
        length = int(declared)
        if length > MAX_BODY:
            raise Oversized(f"A command may not exceed {MAX_BODY} bytes")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"The request body is not valid JSON: {exc}") from exc

    def _record_status(self, identifier, query):
        """Append one status event, or refuse and append nothing.

        The compare-and-append lives in record_status(), which reads the newest event
        inside the write transaction and refuses there. Nothing here re-implements that
        comparison: a second one at this layer could only be read outside the transaction,
        which is the race it exists to close.
        """
        repository = self.server.repository
        try:
            accepted(parse_qs(query, keep_blank_values=True), ())
            supplied = command(self._body())
        except Oversized as refused:
            return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, str(refused))
        except ValueError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        try:
            # Absence decided first, exactly as the read routes decide it, so an id naming
            # nothing is a 404 rather than the repository's own refusal.
            repository.opportunity(identifier)
        except KeyError:
            return self._error(HTTPStatus.NOT_FOUND, "No record with that id")
        try:
            recorded = repository.record_status(
                identifier,
                supplied["status"],
                actor=OPERATOR,
                reason=supplied["reason"],
                expected_event_id=supplied["expected_event_id"],
            )
        except StatusConflict as conflict:
            # Caught by type, never by reading a message. The request was well formed and
            # the operator's authority was real; what moved was the state they acted on,
            # which is a different answer from "this request was wrong".
            return (
                HTTPStatus.CONFLICT,
                JSON,
                json.dumps(
                    {
                        "error": "status_conflict",
                        "expected_event_id": conflict.expected,
                        "observed_event_id": conflict.observed,
                        "status": conflict.status,
                    }
                ).encode("utf-8"),
                None,
            )
        except ValueError as exc:
            return self._error(HTTPStatus.BAD_REQUEST, str(exc))
        return (HTTPStatus.OK, JSON, json.dumps(recorded).encode("utf-8"), None)

    def _static(self, path):
        name = "index.html" if path == "/" else path[1:]
        asset = self.server.assets.get(name)
        if asset is None:
            return self._error(HTTPStatus.NOT_FOUND, "No such resource")
        payload, media = asset
        return (HTTPStatus.OK, media, payload, None)

    # --- responses ------------------------------------------------------------------------

    def _error(self, status, message):
        return (status, JSON, json.dumps({"error": message}).encode("utf-8"), None)

    def _send(self, status, media, payload, extra, *, body):
        self.send_response(status.value)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(payload)))
        for name, value in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(name, value)
        # No Access-Control-Allow-* header is ever sent. Another origin is not refused and
        # then quietly permitted by a header that hands it the answer anyway.
        self.end_headers()
        if body:
            self.wfile.write(payload)


class Surface(ThreadingHTTPServer):
    """One launch: one loopback socket, one token, one repository handle.

    Threading is what keeps a slow read from blocking the pane beside it, and no sqlite3
    object crosses those threads: Repository holds a path and nothing else, and every one
    of its methods opens and closes its own connection on the thread that called it. That
    is why the handle can be shared where a connection could not be, and it is also why
    this class creates no pool and never weakens check_same_thread.
    """

    daemon_threads = True

    def __init__(
        self,
        repository,
        *,
        port=DEFAULT_PORT,
        provider=DEFAULT_PROVIDER,
        provider_namespace=DEFAULT_PROVIDER,
    ):
        self.repository = repository
        # Two strings, decided at launch and never afterwards. They arrive already chosen
        # by the caller that parsed the command line, so nothing here imports a provider,
        # constructs a credential, or learns what reaching that destination would involve.
        self.decision_target = (provider, provider_namespace)
        # Fresh for every launch, from the system's own entropy, and never written down.
        # No parameter accepts one from a caller: a token that could be supplied is a
        # token that could be reused.
        self.token = secrets.token_urlsafe(TOKEN_BYTES)
        self.assets = assets()
        super().__init__((LOOPBACK, port), Handler)
        # Read back from the socket rather than echoed from the request, so port 0 is as
        # authoritative as a fixed one.
        self.authority = f"{LOOPBACK}:{self.server_port}"
        self.origin = f"http://{self.authority}"

    @property
    def launch_url(self) -> str:
        """The token rides in the fragment, which is never sent in an HTTP request.

        In the query string it would reach the server, the request line, and every log and
        history that copies one. After the `#` it stays in the browser, where the bootstrap
        moves it into session storage and clears it from the address bar.
        """
        return f"{self.origin}/#token={self.token}"


def serve(
    repository,
    *,
    port=DEFAULT_PORT,
    provider=DEFAULT_PROVIDER,
    provider_namespace=DEFAULT_PROVIDER,
    announce=print,
) -> None:
    """Bind, print where to go, and serve until interrupted.

    The URL is printed rather than opened. Launching a browser is convenience, and it does
    not belong in the same change as the network boundary.

    `provider` and `provider_namespace` are two strings: where an approval recorded here
    says a draft may go. They are announced, because a destination an operator cannot see
    is one they cannot check, and they are fixed for the process -- a restart issues a new
    token anyway, so a page left open cannot act against a target chosen after it loaded.
    """
    surface = Surface(
        repository, port=port, provider=provider, provider_namespace=provider_namespace
    )
    announce(f"CareerSignal is reading {surface.repository.path}")
    announce(f"Approvals recorded here will name {provider_namespace}")
    announce(f"Open {surface.launch_url}")
    announce("This address is valid for this process only. Stop with Ctrl-C.")
    with surface:
        try:
            surface.serve_forever()
        except KeyboardInterrupt:
            announce("")
