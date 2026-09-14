"""A loopback-only, token-authenticated, read-only HTTP projection of what is stored.

This is the first CareerSignal surface that listens on a socket, so it is defined by what
it refuses. It binds 127.0.0.1 and offers no way to bind anything else. It answers GET and
HEAD and refuses every other method before routing, so no request shape can reach a write.
It reaches the repository's read projections and nothing else: no intake, no decision, no
claim, no provider, no credential. Write authority is a later change and is deliberately
not reachable from here.

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


def session(repository) -> dict:
    """What this machine is set up with. Presence booleans only, never a credential value."""
    return {
        **sqlite_report(),
        "database": str(repository.path),
        "gmail_token": bool(os.getenv(TOKEN_VARIABLE, "").strip()),
        "gmail_compose_token": bool(os.getenv(COMPOSE_TOKEN_VARIABLE, "").strip()),
    }


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
    accepted(query, FILTERS)
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
        """Every method but GET and HEAD, including verbs this has never heard of.

        Answering here rather than enumerating POST, PUT, PATCH and DELETE means a request
        does not need to be anticipated to be refused: the refusal is the default, and
        reaching a route is what has to be spelled out.
        """
        if name.startswith("do_"):
            return self._refuse_method
        raise AttributeError(name)

    def _refuse_method(self):
        self._send(
            HTTPStatus.METHOD_NOT_ALLOWED,
            JSON,
            json.dumps({"error": "This surface is read-only"}).encode("utf-8"),
            body=True,
            extra={"Allow": "GET, HEAD"},
        )

    def do_GET(self):
        self._send(*self._resolve(), body=True)

    def do_HEAD(self):
        self._send(*self._resolve(), body=False)

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
        if parsed.path == API_ROOT or parsed.path.startswith(API_ROOT + "/"):
            return self._api(parsed)
        return self._static(parsed.path)

    def _authorized(self) -> bool:
        presented = self.headers.get(TOKEN_HEADER, "")
        # Compared as bytes so a header carrying anything but ASCII is a mismatch rather
        # than an exception, and in constant time either way.
        return secrets.compare_digest(
            presented.encode("utf-8", "surrogateescape"), self.server.token.encode("utf-8")
        )

    def _api(self, parsed):
        if not self._authorized():
            return self._error(HTTPStatus.UNAUTHORIZED, "A valid launch token is required")
        segments = [unquote(part) for part in parsed.path[len(API_ROOT) :].split("/") if part]
        try:
            payload = self._projection(segments, parse_qs(parsed.query))
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
                return session(repository)
            case ["opportunities"]:
                return repository.opportunities(**filters(query))
            case ["opportunities", identifier]:
                accepted(query, ())
                return repository.opportunity(identifier)
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
        return None

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

    def __init__(self, repository, *, port=DEFAULT_PORT):
        self.repository = repository
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


def serve(repository, *, port=DEFAULT_PORT, announce=print) -> None:
    """Bind, print where to go, and read until interrupted.

    The URL is printed rather than opened. Launching a browser is convenience, and it does
    not belong in the same change as the network boundary.
    """
    surface = Surface(repository, port=port)
    announce(f"CareerSignal is reading {surface.repository.path}")
    announce(f"Open {surface.launch_url}")
    announce("This address is valid for this process only. Stop with Ctrl-C.")
    with surface:
        try:
            surface.serve_forever()
        except KeyboardInterrupt:
            announce("")
