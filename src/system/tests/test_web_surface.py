"""The first CareerSignal surface that listens on a socket, tested by what it refuses.

Everything here is a boundary test. The projections themselves are proved where they live;
what is at stake in this package is narrower and more dangerous: a socket on the operator's
machine that answers questions about their job search, holding a credential that authorizes
a mailbox. So these tests ask who can reach it, what it will answer, what it will never
answer, and whether anything it serves can be made to write.

The rule the whole file exists to hold is that this surface adds nothing. Every route hands
back a repository projection unchanged, and the last test in the file reads the package's
own source to say so mechanically, because a route that quietly starts deciding something
is exactly the failure a passing response body cannot show.
"""

import ast
import hashlib
import http.client
import json
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from importlib import resources
from pathlib import Path
from urllib.parse import quote

import pytest

from communications.gmail import TOKEN_VARIABLE
from communications.gmail_draft import COMPOSE_TOKEN_VARIABLE
from communications.message import Message
from data import store
from data.repository import Repository
from recruiting.models import Profile
from recruiting.status import STATUSES
from system import cli, views
from system.web import server as web
from system.workflow import Intake

JOB_TEXT = (
    "Title: IAM Architect\r\n"
    "Company: Example Corp\r\n"
    "Location: remote\r\n"
    "Skills: Python, SQL\r\n"
    "URL: https://jobs.example.com/roles/1\r\n"
)
# Every method this surface does not answer. POST is deliberately absent: it is answered,
# only at explicitly permitted command addresses, which
# `test_post_reaches_no_address_but_a_permitted_command` covers.
UNANSWERED_METHODS = ("PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "FROBNICATE")


def alert(sender="recruiter@example.com", external_id="m1"):
    return Message(
        namespace="gmail:operator@example.com",
        external_id=external_id,
        sender=sender,
        subject="A role for you",
        text=JOB_TEXT,
    )


@pytest.fixture
def repository(tmp_path):
    store = Repository(tmp_path / "db")
    Intake(store, Profile(("python", "sql"))).intake_message(alert())
    return store


class Client:
    """A raw HTTP client, so a test can send what a browser or an attacker would.

    urllib normalises a path before it leaves, which would quietly repair the very requests
    the traversal and Host tests are about. This sends the request line as written.
    """

    def __init__(self, surface):
        self.surface = surface

    def send(
        self,
        path,
        *,
        method="GET",
        token=True,
        host=None,
        origin=None,
        content_type=None,
        length=None,
        headers=(),
        body=None,
    ):
        connection = http.client.HTTPConnection(web.LOOPBACK, self.surface.server_port, timeout=10)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            connection.putheader("Host", self.surface.authority if host is None else host)
            if origin:
                connection.putheader("Origin", self.surface.origin if origin is True else origin)
            if token:
                connection.putheader(
                    web.TOKEN_HEADER,
                    self.surface.token if token is True else token,
                )
            if content_type:
                connection.putheader("Content-Type", content_type)
            for name, value in headers:
                connection.putheader(name, value)
            if body is not None and length != "omit":
                # `length` lets a test declare something other than the truth, which is the
                # only way to reach the ceiling without actually sending the bytes.
                connection.putheader("Content-Length", str(len(body) if length is None else length))
            connection.endheaders(body)
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def json(self, path, **kwargs):
        status, payload, headers = self.send(path, **kwargs)
        return status, json.loads(payload)

    def unread(self, path, **kwargs):
        """Send a body to an address that refuses before reading it, and report the status.

        This surface settles a refusal without touching the body, which is the property the
        callers below are about. That leaves the sent bytes unread in the receive buffer,
        and closing on unread bytes is a reset rather than a clean shutdown on Windows -- so
        whether the refusal or the reset reaches the client first is a race no test should
        be made to win. `None` means the response was lost to a reset, which is itself only
        possible if the body went unread.

        What a caller asserts either way is that nothing was written. A body that *would*
        have been appended had the address matched makes that assertion the strong one: no
        reset can hide a row, because the digest is read from the database afterwards.
        """
        try:
            return self.send(path, **kwargs)[0]
        except ConnectionError:
            return None

    def command(self, opportunity, payload, **kwargs):
        """A well-formed status command, so a test varies only what it is about."""
        settings = {
            "method": "POST",
            "origin": True,
            "content_type": "application/json",
            "body": json.dumps(payload).encode("utf-8"),
        } | kwargs
        status, body, _ = self.send(f"/api/v1/opportunities/{opportunity}/status", **settings)
        return status, json.loads(body)


@pytest.fixture
def surface(repository):
    running = web.Surface(repository, port=0)
    # A short poll interval only so shutdown() is noticed promptly; serve() uses the
    # stdlib default, and nothing here depends on the interval's value.
    thread = threading.Thread(target=running.serve_forever, kwargs={"poll_interval": 0.02})
    thread.daemon = True
    thread.start()
    try:
        yield running
    finally:
        running.shutdown()
        running.server_close()
        thread.join(timeout=10)


@pytest.fixture
def client(surface):
    return Client(surface)


def package_modules():
    """Every module in the package, parsed.

    Read as syntax rather than as text: the module explains several of these rules in its
    own docstrings, and a substring search cannot tell a rule being stated from a rule
    being broken.
    """
    root = Path(__file__).resolve().parents[1] / "web"
    modules = sorted(root.rglob("*.py"))
    assert modules, "the web package moved and this guard stopped guarding anything"
    return [
        (path.relative_to(root).as_posix(), ast.parse(path.read_text(encoding="utf-8")))
        for path in modules
    ]


def imports() -> set:
    """Every module name this package imports, however it spells the import."""
    found = {node.module or "" for name, node in nodes(ast.ImportFrom)}
    return found | {alias.name for name, node in nodes(ast.Import) for alias in node.names}


def _named(node, identifier) -> bool:
    return isinstance(node, ast.Name) and node.id == identifier


def nodes(kind):
    for name, tree in package_modules():
        for node in ast.walk(tree):
            if isinstance(node, kind):
                yield name, node


def state(repository) -> str:
    """A digest of everything an operator write would move.

    Not the database file: under WAL a write can land entirely in the sidecar and leave the
    main file byte-identical, so file bytes would agree with a mutation that happened. This
    reads the ledgers instead -- both timelines, every status, every decision, every intent.
    """
    reviews = [row["review"] for row in repository.opportunities()]
    facts = {
        "opportunities": repository.opportunities(),
        "timeline": repository.timeline(),
        "authorization": {review: repository.authorization(review) for review in reviews},
        "intents": {review: repository.intent(review) for review in reviews},
    }
    return hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()


# --- where it listens ---------------------------------------------------------------------------


def test_the_surface_binds_loopback_and_offers_no_way_to_bind_anything_else(surface):
    """Not "defaults to loopback": there is no other address it can be asked for."""
    assert surface.server_address[0] == web.LOOPBACK
    assert surface.socket.family == socket.AF_INET
    assert "host" not in web.Surface.__init__.__code__.co_varnames
    bind = next(
        node
        for name, node in nodes(ast.Call)
        if isinstance(node.func, ast.Attribute) and node.func.attr == "__init__"
    )
    # The address is a literal reference to LOOPBACK, not a default a caller could displace.
    assert isinstance(bind.args[0], ast.Tuple)
    assert isinstance(bind.args[0].elts[0], ast.Name) and bind.args[0].elts[0].id == "LOOPBACK"
    for name, node in nodes(ast.Constant):
        assert node.value not in ("0.0.0.0", "::", "localhost"), f"{name}: {node.value!r}"


def test_the_bound_port_is_read_from_the_socket_not_assumed(repository):
    """Port 0 is a real launch, so the authority has to come back from the socket."""
    running = web.Surface(repository, port=0)
    try:
        assert running.server_port != 0
        assert running.authority == f"{web.LOOPBACK}:{running.server_port}"
        assert running.origin == f"http://{running.authority}"
        assert running.launch_url.startswith(running.origin + "/#")
    finally:
        running.server_close()


# --- the launch token ---------------------------------------------------------------------------


def test_every_launch_mints_its_own_token(repository):
    tokens = set()
    for _ in range(3):
        running = web.Surface(repository, port=0)
        tokens.add(running.token)
        running.server_close()
    assert len(tokens) == 3
    assert all(len(token) >= 32 for token in tokens)


def test_the_token_cannot_be_supplied_by_a_caller(repository):
    """A token an argument could set is a token a script could pin and reuse."""
    assert "token" not in web.Surface.__init__.__code__.co_varnames[1:]
    with pytest.raises(TypeError):
        web.Surface(repository, port=0, token="chosen-in-advance")


def test_the_launch_url_carries_the_token_after_the_fragment(surface):
    """A query string reaches the server, the request line, and every log that copies one."""
    assert surface.launch_url == f"{surface.origin}/#token={surface.token}"
    assert "?" not in surface.launch_url
    assert surface.token not in surface.launch_url.split("#")[0]


def test_no_response_ever_reports_the_token(client, surface):
    """Including the refusals, which is where a helpful error would leak it."""
    for path in (
        "/",
        "/app.js",
        "/api/v1/session",
        "/api/v1/opportunities",
        "/api/v1/timeline",
        "/api/v1/nothing-here",
    ):
        for token in (True, False, "wrong"):
            status, payload, headers = client.send(path, token=token)
            assert surface.token.encode() not in payload, (path, status)
            assert surface.token not in json.dumps(headers), (path, status)


def test_the_token_is_not_written_to_the_database_or_to_a_log(client, surface, repository, capsys):
    assert client.send("/api/v1/session")[0] == 200
    assert client.send("/api/v1/session", token="wrong")[0] == 401
    for path in Path(repository.path).parent.iterdir():
        if path.is_file():
            assert surface.token.encode() not in path.read_bytes(), path
    captured = capsys.readouterr()
    # The handler logs nothing at all, which is how the token cannot outlive the process
    # in a file the operator never thinks about.
    assert captured.out == "" and captured.err == ""


def test_an_api_request_without_a_valid_token_is_refused_and_answers_nothing(client, repository):
    for token in (False, "", "wrong", "x" * 43):
        status, body = client.json("/api/v1/opportunities", token=token)
        assert status == 401
        assert body == {"error": "A valid launch token is required"}
    assert repository.opportunities(), "the fixture has nothing to withhold"
    # Constant time, asserted mechanically because a plain `==` refuses exactly the same
    # requests and differs only in what it leaks while doing so.
    called = {
        node.func.attr for name, node in nodes(ast.Call) if isinstance(node.func, ast.Attribute)
    }
    assert "compare_digest" in called


def test_a_token_that_is_not_ascii_is_a_mismatch_rather_than_a_fault(client):
    """Compared as bytes: a header the server cannot even encode must not raise."""
    status, body = client.json("/api/v1/session", token="tökén-with-accents")
    assert status == 401 and "error" in body


def test_the_static_page_needs_no_token_but_carries_no_data(client, surface):
    """The bootstrap has to load before it can present anything; it is inert until it does."""
    status, payload, headers = client.send("/", token=False)
    assert status == 200 and headers["Content-Type"].startswith("text/html")
    assert b'<script type="module" src="app.js"' in payload
    assert surface.token.encode() not in payload
    for name in ("recruiter@example.com", "IAM Architect", "Example Corp"):
        assert name.encode() not in payload, "a static asset carries stored evidence"


# --- who may ask --------------------------------------------------------------------------------


def test_an_unexpected_host_is_refused_before_anything_is_routed(client):
    """The shape DNS rebinding takes: a name the attacker owns, pointed at loopback."""
    for host in ("evil.example.com", "localhost:1", f"careersignal.example.com:{80}"):
        status, body = client.json("/api/v1/session", host=host)
        assert status == 403 and body == {"error": "Unexpected Host"}
    assert client.send("/", host="evil.example.com")[0] == 403


def test_an_unexpected_origin_is_refused_and_the_surfaces_own_origin_is_not(client, surface):
    assert client.json("/api/v1/session", headers=[("Origin", surface.origin)])[0] == 200
    for origin in ("http://evil.example.com", "null", f"https://{surface.authority}"):
        status, body = client.json("/api/v1/session", headers=[("Origin", origin)])
        assert status == 403 and body == {"error": "Unexpected Origin"}, origin


def test_no_response_carries_a_cross_origin_allowance(client, surface):
    """A refusal followed by a permissive header would hand over the answer anyway."""
    for path, kwargs in (
        ("/api/v1/session", {}),
        ("/api/v1/session", {"token": False}),
        ("/api/v1/session", {"headers": [("Origin", "http://evil.example.com")]}),
        ("/api/v1/session", {"method": "POST"}),
        ("/", {"token": False}),
        ("/missing.css", {}),
    ):
        headers = client.send(path, **kwargs)[2]
        assert not [name for name in headers if name.casefold().startswith("access-control")]


# --- what it will not do -------------------------------------------------------------------------


@pytest.mark.parametrize("method", UNANSWERED_METHODS)
def test_every_method_but_the_three_answered_is_refused_without_reaching_a_route(
    client, repository, method
):
    """Including verbs this server has never heard of: refusal is still the default.

    POST is answered, but only at explicitly permitted command addresses. Every other
    method is refused before anything is routed, authenticated or read, which is why this
    list includes verbs nobody has implemented.
    """
    before = state(repository)
    # No body, so the refusal is the only thing in flight and this can assert it exactly.
    # That a body is refused unread is the test below, where it cannot be asserted exactly.
    status, payload, headers = client.send(method=method, path="/api/v1/opportunities")
    assert status == 405
    assert headers["Allow"] == "GET, HEAD, POST"
    assert json.loads(payload) == {"error": "This surface reads, and records a status"}
    assert state(repository) == before


@pytest.mark.parametrize("method", UNANSWERED_METHODS)
def test_an_unanswered_verb_carrying_a_command_shaped_body_writes_nothing(
    client, repository, opportunity, method
):
    """The body of a refused method is never read, so it can never be acted on.

    The payload is the one that would genuinely append if this verb reached the command, so
    the digest is what proves it did not. The response is not asserted: a refusal settled
    without reading the body leaves those bytes unread, and on Windows the close that
    follows resets the connection, which can outrun the response. Nothing about that race
    changes whether a row was written.
    """
    event = repository.opportunity(opportunity)["status_event"]
    before = state(repository)
    status = client.unread(
        f"/api/v1/opportunities/{opportunity}/status",
        method=method,
        origin=True,
        content_type="application/json",
        body=json.dumps({"status": "applied", "expected_event_id": event}).encode("utf-8"),
    )
    assert status in (405, None), status
    assert state(repository) == before


@pytest.mark.parametrize("method", UNANSWERED_METHODS)
def test_no_unanswered_verb_becomes_reachable_with_a_valid_token(client, surface, method):
    """Authority is not the question: there is no handler for a valid token to reach."""
    assert client.send(method=method, path="/api/v1/opportunities")[0] == 405


def test_an_unanswered_verb_is_refused_before_the_token_is_even_considered(client):
    """Earliest possible refusal: a cross-origin PUT never gets as far as 401 or 403."""
    status, _, headers = client.send(
        "/api/v1/session", method="PUT", token=False, host="evil.example.com"
    )
    assert status == 405 and headers["Allow"] == "GET, HEAD, POST"


def test_post_reaches_no_address_but_a_permitted_command(client, repository):
    """One command, not a command router.

    A POST anywhere else is refused with the read methods, which also keeps a POST from
    reporting which read routes exist.
    """
    opportunity = repository.opportunities()[0]["id"]
    before = state(repository)
    for path in (
        "/api/v1/session",
        "/api/v1/opportunities",
        f"/api/v1/opportunities/{opportunity}",
        f"/api/v1/opportunities/{opportunity}/sources",
        "/api/v1/communications",
        "/api/v1/timeline",
        "/api/v1/statuses",
        "/api/v1/opportunities/status",
        f"/api/v1/opportunities/{opportunity}/status/extra",
        f"/api/v1/reviews/{repository.opportunity(opportunity)['review']}/status",
        "/",
    ):
        # Body-free, so the refusal is the only thing in flight and Allow can be asserted
        # exactly. That a command-shaped body is refused unread is pinned separately.
        status, payload, headers = client.send(path, method="POST", origin=True)
        assert status == 405, path
        assert headers["Allow"] == "GET, HEAD", path
        assert json.loads(payload) == {"error": "No command at this address"}, path
    assert state(repository) == before


def test_the_command_address_is_not_reachable_by_a_lookalike_prefix(
    client, repository, opportunity
):
    """`/api/v1` is proven, not assumed.

    Slicing a path by `len(API_ROOT)` without checking the prefix strips any seven
    characters, so every seven-character prefix would present the command's own segments.
    The body here is the one that would genuinely append -- it names the current event --
    so a regression answers 200 and writes, rather than failing on a malformed payload for
    some unrelated reason.
    """
    event = repository.opportunity(opportunity)["status_event"]
    payload = json.dumps({"status": "interested", "expected_event_id": event}).encode("utf-8")
    before = state(repository)
    for path in (
        f"/abcdef/opportunities/{opportunity}/status",
        f"/1234567/opportunities/{opportunity}/status",
        f"/../../x/opportunities/{opportunity}/status",
        f"/api/v2/opportunities/{opportunity}/status",
        f"/API/V1/opportunities/{opportunity}/status",
        f"/api/v1x/opportunities/{opportunity}/status",
        f"/x/api/v1/opportunities/{opportunity}/status",
    ):
        status = client.unread(
            path, method="POST", origin=True, content_type="application/json", body=payload
        )
        assert status in (405, None), (path, status)
    assert state(repository) == before


def test_the_command_address_is_singular_rather_than_a_family_of_aliases(
    client, repository, opportunity
):
    """A doubled or trailing slash is a different address, not another spelling of this one.

    Each command is reached at exactly one address. Filtering empty path components out
    would quietly make several spellings equal, which is how an address that was reasoned
    about once ends up with variants nobody reasoned about.
    """
    event = repository.opportunity(opportunity)["status_event"]
    payload = json.dumps({"status": "interested", "expected_event_id": event}).encode("utf-8")
    before = state(repository)
    for path in (
        f"/api/v1//opportunities/{opportunity}/status",
        f"/api/v1/opportunities//{opportunity}/status",
        f"/api/v1/opportunities/{opportunity}//status",
        f"/api/v1/opportunities/{opportunity}/status/",
    ):
        status = client.unread(
            path, method="POST", origin=True, content_type="application/json", body=payload
        )
        assert status in (405, None), (path, status)
    assert state(repository) == before
    # And the canonical spelling still works, so the rule above narrowed nothing it should
    # not have.
    assert (
        client.command(opportunity, {"status": "interested", "expected_event_id": event})[0] == 200
    )


def test_a_target_naming_its_own_authority_is_refused(client, surface, repository, opportunity):
    """Identity is settled on Host, so a target may not name an authority of its own.

    In absolute form the target carries the authority, and HTTP makes *that* the one
    identifying the server. This surface checks the Host header instead, so honouring
    absolute form would route by one authority while checking another -- and would give the
    single command address a second spelling that reaches the same write.
    """
    event = repository.opportunity(opportunity)["status_event"]
    payload = json.dumps({"status": "interested", "expected_event_id": event}).encode("utf-8")
    before = state(repository)
    for target in (
        f"http://{surface.authority}/api/v1/opportunities/{opportunity}/status",
        f"https://{surface.authority}/api/v1/opportunities/{opportunity}/status",
        f"http://elsewhere.example/api/v1/opportunities/{opportunity}/status",
    ):
        status = client.unread(
            target, method="POST", origin=True, content_type="application/json", body=payload
        )
        assert status in (400, None), (target, status)
    assert state(repository) == before
    # The reads refuse it on the same rule rather than a separate one.
    assert client.send(f"http://{surface.authority}/api/v1/session")[0] == 400


def test_a_read_address_is_singular_too(client, repository, opportunity):
    """The same parser serves the reads, so the same spellings are the same one address."""
    for path in (
        "/api/v1//session",
        "/api/v1/session/",
        "/api/v1//opportunities",
        f"/api/v1/opportunities/{opportunity}/sources/",
    ):
        status, _, _ = client.send(path)
        assert status == 404, path
    assert client.send("/api/v1/session")[0] == 200


@pytest.mark.parametrize(
    "path",
    (
        "/../server.py",
        "/../../pyproject.toml",
        "/%2e%2e/server.py",
        "/%2e%2e%2fserver.py",
        "/static/index.html",
        "/./index.html",
        "//etc/passwd",
        "/",
        "",
    ),
)
def test_nothing_outside_the_packaged_asset_names_can_be_served(client, path):
    """A name, never a path.

    The request is compared against the exact names the package ships, so there is no
    traversal to defeat -- `..` is a key that does not exist. `/` and the empty path are in
    the list because they are the two spellings that must land on the page itself.
    """
    status, payload, headers = client.send(path or "/", token=False)
    if path in ("/", ""):
        assert status == 200 and headers["Content-Type"].startswith("text/html")
    else:
        assert status == 404, path
        assert json.loads(payload) == {"error": "No such resource"}


def test_the_assets_are_read_from_package_resources_not_from_the_checkout(surface):
    """An installed wheel has no checkout beside it; the wheel test proves the other half."""
    assert set(surface.assets) == {
        "index.html",
        "careersignal.css",
        "app.js",
        "api.js",
        "dom.js",
        "screens.js",
        "icon.svg",
    }
    packaged = resources.files("system.web") / "static"
    for name, (payload, media) in surface.assets.items():
        assert payload == (packaged / name).read_bytes()
        assert media == web.MEDIA_TYPES[name[name.rfind(".") :]]
    # Nothing locates an asset relative to this checkout: no __file__, and no path library
    # to join one with.
    assert not [node for name, node in nodes(ast.Name) if node.id == "__file__"]
    assert "pathlib" not in imports()
    # `os` is imported to read two environment variables and for nothing else; in
    # particular not to join, resolve or walk a path.
    assert {node.attr for name, node in nodes(ast.Attribute) if _named(node.value, "os")} == {
        "getenv"
    }


def test_an_asset_with_no_declared_media_type_stops_the_launch(monkeypatch):
    """Guessing would mean serving unknown bytes under a nosniff header."""
    monkeypatch.setitem(web.MEDIA_TYPES, ".css", None)
    monkeypatch.delitem(web.MEDIA_TYPES, ".css")
    with pytest.raises(RuntimeError, match="media type"):
        web.assets()


# --- what it answers ------------------------------------------------------------------------------


def test_the_session_facts_report_presence_and_never_a_credential(client, monkeypatch, repository):
    monkeypatch.setenv(TOKEN_VARIABLE, "ya29-not-a-real-gmail-credential")
    monkeypatch.setenv(COMPOSE_TOKEN_VARIABLE, "  ")
    status, body = client.json("/api/v1/session")
    assert status == 200
    assert body["gmail_token"] is True and body["gmail_compose_token"] is False
    assert body["database"] == str(repository.path)
    assert "not-a-real-gmail-credential" not in json.dumps(body)
    assert set(body) == {
        "sqlite",
        "minimum",
        "wal_reset_fix",
        "nearest_wal_reset_fix",
        "database",
        "gmail_token",
        "gmail_compose_token",
        "decision_target",
        "outward",
    }
    # A name, never a credential: the default launch declares the local provider, and the
    # token set above must not appear anywhere in the target it reports.
    assert body["decision_target"] == {
        "provider": "controlled",
        "provider_namespace": "controlled",
    }


def test_every_route_is_the_repository_projection_unchanged(client, repository):
    """The whole point of the package, asserted route by route.

    Equality against the repository's own return value, not against a literal: a route that
    reformatted, re-sorted or re-derived anything would be a second opinion about state the
    storage layer already settled, and the two would drift apart on the first change.
    """
    opportunity = repository.opportunities()[0]["id"]
    review = repository.opportunity(opportunity)["review"]
    message = repository.communications()[0]["message"]
    for path, expected in (
        ("/api/v1/opportunities", repository.opportunities()),
        (f"/api/v1/opportunities/{opportunity}", repository.opportunity(opportunity)),
        (f"/api/v1/opportunities/{opportunity}/sources", repository.sources(opportunity)),
        ("/api/v1/communications", repository.communications()),
        (f"/api/v1/communications/{message}", repository.communication(message)),
        (f"/api/v1/reviews/{review}/authorization", repository.authorization(review)),
        ("/api/v1/timeline", repository.timeline()),
    ):
        status, body = client.json(path)
        assert (status, body) == (200, json.loads(json.dumps(expected))), path


def test_the_filters_are_the_arguments_the_repository_already_validates(client, repository):
    opportunity = repository.opportunities()[0]["id"]
    repository.record_status(opportunity, "interested", actor="operator", reason="")
    for query, expected in (
        ("status=interested", repository.opportunities(status="interested")),
        ("status=new", repository.opportunities(status="new")),
        ("active=true", repository.opportunities(active=True)),
        ("eligible=true", repository.opportunities(eligible=True)),
        ("eligible=false", repository.opportunities(eligible=False)),
        (
            "min_coverage=0&max_coverage=100",
            repository.opportunities(min_coverage=0, max_coverage=100),
        ),
        ("min_coverage=101", None),
    ):
        status, body = client.json("/api/v1/opportunities?" + query)
        if expected is None:
            assert status == 400, query
        else:
            assert (status, body) == (200, json.loads(json.dumps(expected))), query
    assert repository.opportunities(status="new") == [], "the fixture no longer distinguishes these"


def test_the_timeline_window_is_the_repository_window(client, repository):
    status, body = client.json("/api/v1/timeline?limit=1")
    assert (status, body) == (200, repository.timeline(limit=1))
    since = repository.timeline()[-1]["created_at"]
    status, body = client.json(f"/api/v1/timeline?since={since}")
    assert (status, body) == (200, repository.timeline(since=since))


def test_a_head_answers_the_same_headers_with_no_body(client):
    for path in ("/", "/api/v1/session", "/api/v1/opportunities", "/nothing.css"):
        expected_status, payload, expected_headers = client.send(path)
        status, empty, headers = client.send(path, method="HEAD")
        assert (status, empty) == (expected_status, b""), path
        assert headers == expected_headers | {"Date": headers["Date"]}
        assert headers["Content-Length"] == str(len(payload)), path


def test_the_security_headers_are_on_every_response_including_the_refusals(client):
    for path, kwargs in (
        ("/", {"token": False}),
        ("/api/v1/session", {}),
        ("/api/v1/session", {"token": False}),
        ("/api/v1/session", {"method": "POST"}),
        ("/api/v1/nope", {}),
        ("/nope.js", {}),
    ):
        headers = client.send(path, **kwargs)[2]
        for name, value in web.SECURITY_HEADERS.items():
            assert headers.get(name) == value, (path, name)
    assert "'unsafe-inline'" not in web.CONTENT_SECURITY_POLICY
    assert "'unsafe-eval'" not in web.CONTENT_SECURITY_POLICY


# --- what it refuses to answer --------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    (
        "/api/v1/opportunities/no-such-opportunity",
        "/api/v1/opportunities/no-such-opportunity/sources",
        "/api/v1/communications/no-such-message",
        "/api/v1/reviews/no-such-review/authorization",
    ),
)
def test_an_id_that_names_nothing_is_not_found(client, path):
    status, body = client.json(path)
    assert status == 404 and body == {"error": "No record with that id"}


def test_an_opportunity_with_no_stored_sources_is_an_empty_list_not_a_refusal(client, repository):
    """Absence of a record and absence of evidence must not arrive looking the same."""
    Intake(repository, Profile(("python", "sql"))).intake(
        "raw-1",
        '{"jobs": [{"title": "Engineer", "company": "Other Corp", '
        '"url": "https://jobs.example.com/roles/2", "location": "remote", '
        '"skills": ["python", "sql"]}]}',
    )
    bare = next(row["id"] for row in repository.opportunities() if row["company"] == "Other Corp")
    assert client.json(f"/api/v1/opportunities/{bare}/sources") == (200, [])
    assert client.json(f"/api/v1/opportunities/{bare}zz/sources")[0] == 404


@pytest.mark.parametrize(
    "path",
    (
        "/api/v1/",
        "/api/v1/session/extra",
        "/api/v1/opportunities/x/y",
        "/api/v1/reviews/x",
        "/api/v1/reviews/x/decision",
        "/api/v1/decide",
        "/api/v1/status",
    ),
)
def test_a_route_this_surface_does_not_have_is_not_found(client, path):
    status, body = client.json(path)
    assert status == 404 and body == {"error": "No such route"}


@pytest.mark.parametrize(
    "path",
    (
        "/api/v1/opportunities?status=not-a-status",
        "/api/v1/opportunities?eligible=yes",
        "/api/v1/opportunities?eligible=true&eligible=false",
        "/api/v1/opportunities?min_coverage=-1",
        "/api/v1/opportunities?min_coverage=one",
        "/api/v1/opportunities?max_coverage=101",
        "/api/v1/opportunities?limit=5",
        "/api/v1/opportunities?Status=new",
        "/api/v1/opportunities?status=",
        "/api/v1/opportunities?eligible=",
        "/api/v1/opportunities?min_coverage=",
        "/api/v1/opportunities?misspelled=",
        "/api/v1/opportunities?unknown=",
        "/api/v1/opportunities?status=new&status=new",
        "/api/v1/opportunities?status=&status=",
        # A blank and a value for the same parameter. The two repeats above cannot catch
        # an implementation that takes whichever of them is non-blank; this one can.
        "/api/v1/opportunities?eligible=&eligible=true",
        "/api/v1/opportunities?eligible=true&eligible=",
        "/api/v1/timeline?limit=",
        "/api/v1/timeline?since=",
        "/api/v1/timeline?limit=0",
        "/api/v1/timeline?limit=-1",
        "/api/v1/timeline?status=new",
        "/api/v1/session?limit=1",
        "/api/v1/communications?limit=1",
    ),
)
def test_a_query_this_route_cannot_mean_is_a_bad_request(client, path):
    """A misspelled filter that returned everything would misreport a narrowed list."""
    status, body = client.json(path)
    assert status == 400, path
    assert set(body) == {"error"} and body["error"]


def test_a_blank_value_is_a_value_and_not_a_missing_parameter(client, repository):
    """The default `parse_qs` drops `?status=`, and a dropped filter is the wrong answer.

    A filter that disappears at the parsing boundary is never validated and never refused:
    the request narrows nothing and comes back with everything, which reads on screen as a
    narrowed list that happens to be long. That is worse than an error, and worse than the
    blank being rejected -- so blanks are kept and then refused by the rules that already
    exist, one layer down.
    """
    everything = client.json("/api/v1/opportunities")
    assert everything[0] == 200 and everything[1], "the fixture has nothing to over-report"

    for query in ("status=", "eligible=", "min_coverage=", "misspelled="):
        status, body = client.json("/api/v1/opportunities?" + query)
        assert status == 400, (query, body)
        assert body != everything[1], "a narrowed request was answered with the whole list"
    assert client.json("/api/v1/timeline?limit=")[0] == 400


def test_a_superseded_review_keeps_the_repositorys_own_refusal(client, repository):
    """Existing but no longer bindable is a 400 in the repository's words, not a 404.

    The two refusals mean different things to whoever is reading. A 404 says the id is
    wrong; this says the id is right and the review it names has been replaced by a newer
    one for the same opportunity. Collapsing them would send an operator looking for a
    typo instead of for the review that superseded theirs.
    """
    opportunity = repository.opportunities()[0]["id"]
    superseded = repository.opportunity(opportunity)["review"]
    assert client.json(f"/api/v1/reviews/{superseded}/authorization")[0] == 200
    Intake(repository, Profile(("python", "sql"))).intake_message(
        Message(
            namespace="gmail:operator@example.com",
            external_id="m2",
            sender="recruiter@example.com",
            subject="A role for you",
            text=JOB_TEXT.replace("Skills: Python, SQL", "Skills: Python, SQL, Terraform"),
        )
    )
    current = repository.opportunity(opportunity)["review"]
    assert current != superseded, "the fixture stopped superseding the review"
    assert repository.review(superseded), "the superseded review is still a real record"

    status, body = client.json(f"/api/v1/reviews/{superseded}/authorization")
    assert status == 400 and body["error"] == "Review is missing or stale"
    assert client.json(f"/api/v1/reviews/{current}/authorization")[0] == 200


# --- presentation ---------------------------------------------------------------------------------


def test_the_established_reads_are_unchanged_unless_presentation_is_asked_for(client, repository):
    """The contract #29 set: a caller that asked for a projection keeps getting one.

    Byte-for-byte against the repository, on every one of the eight reads, including the two
    that can be enriched. Enrichment is opt-in precisely so this stays true.
    """
    opportunity = repository.opportunities()[0]["id"]
    for path in (
        "/api/v1/opportunities",
        f"/api/v1/opportunities/{opportunity}",
        "/api/v1/opportunities?presentation=false",
        f"/api/v1/opportunities/{opportunity}?presentation=false",
    ):
        status, body = client.json(path)
        assert status == 200, path
        rows = body if isinstance(body, list) else [body]
        assert all("presentation" not in row for row in rows), path
    assert client.json("/api/v1/opportunities")[1] == json.loads(
        json.dumps(repository.opportunities())
    )


def test_presentation_is_exactly_the_python_that_owns_it(client, repository):
    """Equality against system.views itself, not against expected strings.

    A literal here would pass while the two drifted; this cannot. The whole reason the
    frontend receives these rather than computing them is that there is one implementation
    of what CareerSignal state means, and it is the one being called on the right.
    """
    status, rows = client.json("/api/v1/opportunities?presentation=true")
    assert status == 200 and rows
    for row in rows:
        action = repository.opportunity(row["id"])["action"]
        assert row["presentation"] == {
            "coverage": views.coverage(row),
            "queue": views.queue(row, action),
            "approval": views.approval(action),
            "attempt": views.attempt(action),
        }, row["company"]
        assert set(row["presentation"]) == {"coverage", "queue", "approval", "attempt"}


def test_presentation_adds_a_namespace_and_changes_nothing_else(client, repository):
    opportunity = repository.opportunities()[0]["id"]
    for path in ("/api/v1/opportunities/" + opportunity, "/api/v1/opportunities"):
        plain = client.json(path)[1]
        enriched = client.json(path + "?presentation=true")[1]
        plain_rows = plain if isinstance(plain, list) else [plain]
        rich_rows = enriched if isinstance(enriched, list) else [enriched]
        assert len(plain_rows) == len(rich_rows)
        for before, after in zip(plain_rows, rich_rows):
            assert {key: value for key, value in after.items() if key != "presentation"} == before


def test_the_detail_presentation_is_the_same_answer_as_the_row(client, repository):
    """A list and a detail pane cannot disagree, which is the whole point of one queue().

    Decided first, and on a second opportunity left undecided, so the two panes are being
    compared on rows where the answer is actually different. Against an all-undecided
    fixture every field would read the same whether or not the two sides shared an
    implementation, and this would pass while proving nothing.
    """
    review = repository.opportunities()[0]["review"]
    repository.decide(review, approved=True, actor="operator")
    Intake(repository, Profile(("python", "sql"))).intake_message(
        Message(
            namespace="gmail:operator@example.com",
            external_id="m9",
            sender="recruiter@example.com",
            subject="Another role",
            text=JOB_TEXT.replace("roles/1", "roles/9").replace("IAM Architect", "Data Engineer"),
        )
    )
    rows = client.json("/api/v1/opportunities?presentation=true")[1]
    assert len({row["presentation"]["queue"] for row in rows}) > 1, (
        "every row is in the same queue, so this cannot tell the two panes apart"
    )
    for row in rows:
        detail = client.json(f"/api/v1/opportunities/{row['id']}?presentation=true")[1]
        assert detail["presentation"] == row["presentation"], row["company"]


@pytest.mark.parametrize(
    "query",
    (
        "presentation=",
        "presentation=yes",
        "presentation=1",
        "presentation=TRUE&presentation=false",
        "presentation=true&presentation=true",
        "Presentation=true",
    ),
)
def test_presentation_follows_the_same_fail_closed_query_rule(client, repository, query):
    opportunity = repository.opportunities()[0]["id"]
    assert client.json("/api/v1/opportunities?" + query)[0] == 400, query
    assert client.json(f"/api/v1/opportunities/{opportunity}?" + query)[0] == 400, query


def test_presentation_is_not_a_parameter_of_the_other_reads(client, repository):
    """It enriches the two reads the contract named, and is unknown everywhere else."""
    message = repository.communications()[0]["message"]
    opportunity = repository.opportunities()[0]["id"]
    for path in (
        "/api/v1/session",
        "/api/v1/communications",
        f"/api/v1/communications/{message}",
        f"/api/v1/opportunities/{opportunity}/sources",
        "/api/v1/timeline",
    ):
        assert client.json(path + "?presentation=true")[0] == 400, path


def test_presentation_composes_with_the_filters(client, repository):
    opportunity = repository.opportunities()[0]["id"]
    repository.record_status(opportunity, "interested", actor="operator", reason="")
    status, rows = client.json("/api/v1/opportunities?status=interested&presentation=true")
    assert status == 200 and len(rows) == 1
    assert rows[0]["id"] == opportunity and "presentation" in rows[0]


def routed_only(vocabulary):
    """Every string from `vocabulary` in this package must be part of a route declaration.

    `draft` and `reconcile` name two outward commands and also two queues. The collision is
    real and cannot be spelled away, so it is resolved by where the string sits rather than by
    excusing the word: inside the command router's match statement, or inside the tuple of
    route segments declared beside the other commands, it is an address. Anywhere else -- a
    comparison, a lookup table, a response body -- it would be this layer forming an opinion
    about what a row means, which is the thing being forbidden.
    """
    for name, tree in package_modules():
        routes = set()
        for node in ast.walk(tree):
            # The declared route segments: COMMAND, DECISION, OUTWARD and anything added
            # beside them, all of which are module-level tuples of plain strings.
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Tuple):
                routes.update(map(id, ast.walk(node.value)))
            # The router itself, where an address is matched rather than interpreted.
            if isinstance(node, ast.Match):
                routes.update(map(id, ast.walk(node)))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in vocabulary:
                assert id(node) in routes, f"{name}: {node.value!r} outside a route declaration"


def test_the_web_package_derives_presentation_only_by_calling_views():
    """Mechanical, because the alternative is a second implementation nobody notices.

    Every `views.` attribute in the package must be one of the four the contract names. A
    fifth -- or a locally written precedence table, status vocabulary or approval rule --
    fails here rather than at review, which is what keeps Python authoritative about what
    CareerSignal state means.
    """
    used = {node.attr for name, node in nodes(ast.Attribute) if _named(node.value, "views")}
    assert used == {"coverage", "queue", "approval", "attempt"}, used
    written = {node.value for name, node in nodes(ast.Constant) if isinstance(node.value, str)}
    assert not written & set(STATUSES), "the status vocabulary is copied into the web layer"
    assert not written & set(views.DRAFTS.values()), "draft wording is copied into the web layer"
    # A queue name in this package would be this layer deciding what a row means. Two of them
    # are also the names of the outward commands, which are addresses rather than states -- so
    # the rule is about position, not spelling: a queue-named string is permitted only where a
    # route is declared, and `routed_only` fails on one used anywhere a decision could be made.
    routed_only(set(views.QUEUES))


# --- hostile content ------------------------------------------------------------------------------

HOSTILE = (
    "<script>alert(1)</script>",
    "<img src=x onerror=alert(1)>",
    "\x1b]0;spoofed\x07",
    "\u202eevil\u200b",
    "quotes \" ' and & < >",
    "first line\nsecond line",
)


def test_stored_evidence_reaches_the_api_exactly_as_it_arrived(repository, client):
    """Storage is untouched; safety is applied where the text is presented.

    The terminal escapes for a terminal and the browser builds text nodes, but neither
    rewrites the record. What a recruiter actually sent stays readable, because being able
    to see what arrived is the whole reason the evidence is kept.
    """
    hostile = "".join(HOSTILE)
    Intake(repository, Profile(("python", "sql"))).intake_message(
        Message(
            namespace="gmail:operator@example.com",
            external_id="hostile",
            sender="recruiter@example.com",
            subject=hostile,
            text=JOB_TEXT.replace("IAM Architect", hostile).replace("roles/1", "roles/hostile"),
        )
    )
    listed = client.json("/api/v1/communications")[1]
    message = next(row for row in listed if row["external_id"] == "hostile")
    assert message["subject"] == hostile, "the subject was rewritten on the way out"

    detail = client.json(f"/api/v1/communications/{message['message']}")[1]
    assert any(hostile in str(item[1]) for item in detail["items"]), "the excerpt was altered"

    rows = client.json("/api/v1/opportunities?presentation=true")[1]
    carried = next(row for row in rows if "<script>alert(1)</script>" in row["title"])
    # A title goes through the domain's own whitespace normalisation on the way in, so it
    # is not byte-identical to the subject. Every dangerous sequence still survives: what
    # is stored is what is shown, and nothing is silently removed at the edge.
    for payload in ("<script>alert(1)</script>", "<img src=x onerror=alert(1)>", "\x1b]0;"):
        assert payload in carried["title"], payload


def test_a_hostile_payload_is_inert_because_of_what_it_is_served_as(client, repository):
    """`</script>` survives json.dumps, and that is not a defect here.

    Escaping it would only matter if a response were embedded inside an HTML script block,
    which this architecture never does: the API is fetched and parsed as JSON, and the page
    builds text nodes. What keeps the payload inert is the content type plus nosniff -- a
    browser is never invited to read this as a document -- and a frontend with no markup
    sink, which the frontend tests prove separately.
    """
    subject = "</script><script>alert(1)</script>"
    Intake(repository, Profile(("python", "sql"))).intake_message(
        Message(
            namespace="gmail:operator@example.com",
            external_id="hostile2",
            sender="recruiter@example.com",
            subject=subject,
            text=JOB_TEXT.replace("roles/1", "roles/hostile2"),
        )
    )
    status, payload, headers = client.send("/api/v1/communications")
    assert status == 200
    assert headers["Content-Type"] == "application/json; charset=utf-8"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert "text/html" not in headers["Content-Type"]
    listed = json.loads(payload)
    assert any(row["subject"] == subject for row in listed), "the subject did not round-trip"


# --- recording a status -----------------------------------------------------------------------


@pytest.fixture
def opportunity(repository):
    return repository.opportunities()[0]["id"]


def history(repository, opportunity):
    return repository.opportunity(opportunity)["history"]


def test_two_commands_carrying_the_same_event_produce_exactly_one_append(repository, surface):
    """The test this whole boundary exists for.

    Two browser writes race with the same expected event, which is what happens when an
    operator has two tabs open, or when a tab acts on a state a CLI command has already
    moved. The compare-and-append is inside the write transaction, so the database decides:
    one appends, one is told the state moved, and the ledger gains exactly one row. Nothing
    in the web server serialises these -- a lock here would be a second concurrency
    authority, and the wrong one.
    """
    opportunity = repository.opportunities()[0]["id"]
    event = repository.opportunity(opportunity)["status_event"]
    before = len(history(repository, opportunity))

    ready = threading.Barrier(2)
    answers = []

    def race(status):
        client = Client(surface)
        ready.wait(timeout=10)
        answers.append(client.command(opportunity, {"status": status, "expected_event_id": event}))

    racers = [
        threading.Thread(target=race, args=("interested",)),
        threading.Thread(target=race, args=("applied",)),
    ]
    for racer in racers:
        racer.start()
    for racer in racers:
        racer.join(timeout=20)
        assert not racer.is_alive(), "a command never returned"

    codes = sorted(code for code, _ in answers)
    assert codes == [200, 409], answers
    conflict = next(body for code, body in answers if code == 409)
    assert conflict["error"] == "status_conflict"
    assert conflict["expected_event_id"] == event
    assert conflict["observed_event_id"] != event, "the refusal names the event it found"

    after = history(repository, opportunity)
    assert len(after) == before + 1, "the ledger gained more or less than one row"
    winner = next(body for code, body in answers if code == 200)
    assert after[-1]["event"] == winner["event"] == conflict["observed_event_id"]
    assert after[-1]["status"] == winner["status"]


@pytest.mark.parametrize(
    "supplied, expected",
    (
        ({"status": "applied", "expected_event_id": True}, "whole number"),
        ({"status": "applied", "expected_event_id": False}, "whole number"),
        ({"status": "applied", "expected_event_id": 3.0}, "whole number"),
        ({"status": "applied", "expected_event_id": "3"}, "whole number"),
        ({"status": b"applied", "expected_event_id": 3}, "status must be text"),
        ({"status": "applied", "expected_event_id": 3, "actor": "x"}, "Unknown field"),
        ([], "must be a JSON object"),
        ("applied", "must be a JSON object"),
    ),
)
def test_the_command_shape_is_refused_at_this_layer_too(supplied, expected):
    """Tested directly, not only through the repository's identical check.

    `record_status()` refuses a bool event id as well, which is defence in depth working --
    and it is also why an HTTP-layer mistake here would be invisible end to end. This calls
    the boundary's own validator so that guard has a test of its own.
    """
    with pytest.raises(ValueError, match=expected):
        web.command(supplied)


def test_the_command_shape_accepts_exactly_the_three_documented_fields():
    assert web.command({"status": "applied", "expected_event_id": 3}) == {
        "status": "applied",
        "reason": "",
        "expected_event_id": 3,
    }
    assert web.command({"status": "applied", "expected_event_id": 3, "reason": "note"}) == {
        "status": "applied",
        "reason": "note",
        "expected_event_id": 3,
    }
    assert web.COMMAND_FIELDS == {"status", "reason", "expected_event_id"}


def test_no_header_or_field_can_name_the_actor(client, repository, opportunity):
    """The unknown-field refusal covers the body; this covers everything else.

    A header is the other way a caller could try to name themselves, and it would not be
    refused as an unknown field because it never reaches the body at all.
    """
    event = repository.opportunity(opportunity)["status_event"]
    status, _ = client.command(
        opportunity,
        {"status": "applied", "expected_event_id": event},
        headers=[("X-Actor", "somebody else"), ("From", "nobody@example.com")],
    )
    assert status == 200
    assert history(repository, opportunity)[-1]["actor"] == "operator"
    # Stated once in the source, as a constant, so there is one place to read the answer.
    assert web.OPERATOR == "operator"
    # Every actor this package names, not a count of them. A count has to be edited each
    # time a command is added, and editing it is exactly how a supplied actor would get in:
    # the number would go up either way. This says what the rule actually is, so a write
    # that named anything else fails here however many writes there are.
    supplied = [
        node
        for name, node in nodes(ast.keyword)
        if node.arg == "actor" and not _named(node.value, "OPERATOR")
    ]
    assert not supplied, "an actor is coming from somewhere other than the module constant"
    assert [node for name, node in nodes(ast.keyword) if node.arg == "actor"], (
        "no actor is named at all; this guard has stopped guarding anything"
    )


def test_a_command_appends_and_says_what_it_appended(client, repository, opportunity):
    event = repository.opportunity(opportunity)["status_event"]
    status, body = client.command(
        opportunity,
        {"status": "interested", "reason": "worth a look", "expected_event_id": event},
    )
    assert status == 200
    assert body == {"status": "interested", "event": body["event"], "previous_event": event}
    recorded = history(repository, opportunity)[-1]
    assert (recorded["status"], recorded["reason"]) == ("interested", "worth a look")
    assert recorded["event"] == body["event"]


def test_the_reason_is_optional_and_defaults_to_nothing(client, repository, opportunity):
    event = repository.opportunity(opportunity)["status_event"]
    assert client.command(opportunity, {"status": "applied", "expected_event_id": event})[0] == 200
    assert history(repository, opportunity)[-1]["reason"] == ""


def test_the_actor_is_the_operator_and_never_comes_from_the_request(
    client, repository, opportunity
):
    """A browser-supplied actor would let presentation input rewrite audit identity.

    It is not ignored, it is refused: silently dropping a field a caller believed in is how
    a surface ends up recording something other than what was asked for.
    """
    event = repository.opportunity(opportunity)["status_event"]
    status, body = client.command(
        opportunity,
        {"status": "applied", "expected_event_id": event, "actor": "somebody else"},
    )
    assert status == 400 and "actor" in body["error"]
    assert history(repository, opportunity)[-1]["actor"] != "somebody else"

    assert client.command(opportunity, {"status": "applied", "expected_event_id": event})[0] == 200
    assert history(repository, opportunity)[-1]["actor"] == "operator"


def test_a_status_recorded_on_the_command_line_moves_the_event_the_browser_holds(
    client, repository, opportunity
):
    """The case expected_event_id exists for, with the two surfaces it actually spans."""
    read = repository.opportunity(opportunity)["status_event"]
    repository.record_status(opportunity, "withdrawn", actor="operator", reason="")

    status, body = client.command(opportunity, {"status": "applied", "expected_event_id": read})
    assert status == 409
    assert body["expected_event_id"] == read
    assert body["status"] == "withdrawn", "the refusal says what is true now"
    assert repository.status(opportunity) == "withdrawn", "the stale command was applied anyway"


def test_a_conflict_writes_nothing(client, repository, opportunity):
    event = repository.opportunity(opportunity)["status_event"]
    assert (
        client.command(opportunity, {"status": "interested", "expected_event_id": event})[0] == 200
    )
    settled = history(repository, opportunity)
    assert client.command(opportunity, {"status": "closed", "expected_event_id": event})[0] == 409
    assert history(repository, opportunity) == settled


@pytest.mark.parametrize(
    "payload, expected",
    (
        ({"status": "applied"}, "expected_event_id is required"),
        ({"expected_event_id": 1}, "status is required"),
        ({"status": "applied", "expected_event_id": True}, "whole number"),
        ({"status": "applied", "expected_event_id": 1.0}, "whole number"),
        ({"status": "applied", "expected_event_id": "1"}, "whole number"),
        ({"status": "applied", "expected_event_id": None}, "whole number"),
        ({"status": 4, "expected_event_id": 1}, "status must be text"),
        ({"status": "applied", "expected_event_id": 1, "reason": 9}, "reason must be text"),
        ({"status": "applied", "expected_event_id": 1, "actor": "x"}, "Unknown field: actor"),
        ({"status": "applied", "expected_event_id": 1, "opportunity": "x"}, "Unknown field"),
        ({"status": "nonsense", "expected_event_id": 1}, "Unknown status"),
        ({}, "is required"),
    ),
)
def test_a_command_this_surface_cannot_mean_is_a_bad_request(
    client, repository, opportunity, payload, expected
):
    # Every case here is refused on shape, before the event is ever compared, so the event
    # id in the payload is deliberately not substituted for the real one. An earlier draft
    # of this test did substitute it, keyed on `== 1` -- which is true of both `True` and
    # `1.0`, the two values it most needed to keep distinct.
    before = state(repository)
    status, body = client.command(opportunity, payload)
    assert status == 400, (payload, body)
    assert expected in body["error"], (payload, body)
    assert state(repository) == before


@pytest.mark.parametrize(
    "raw",
    (b"{not json", b'"a string"', b"[1, 2, 3]", b"null", b"42", b"", b"true"),
)
def test_a_body_that_is_not_a_json_object_is_refused(client, repository, opportunity, raw):
    before = state(repository)
    status, payload, _ = client.send(
        f"/api/v1/opportunities/{opportunity}/status",
        method="POST",
        origin=True,
        content_type="application/json",
        body=raw,
    )
    assert status == 400, raw
    assert "error" in json.loads(payload)
    assert state(repository) == before


def test_a_command_must_be_sent_as_json(client, repository, opportunity):
    event = repository.opportunity(opportunity)["status_event"]
    body = json.dumps({"status": "applied", "expected_event_id": event}).encode()
    for content_type in (None, "text/plain", "application/x-www-form-urlencoded", "text/json"):
        status, payload, _ = client.send(
            f"/api/v1/opportunities/{opportunity}/status",
            method="POST",
            origin=True,
            content_type=content_type,
            body=body,
        )
        assert status == 400, content_type
        assert "application/json" in json.loads(payload)["error"], content_type
    assert (
        client.send(
            f"/api/v1/opportunities/{opportunity}/status",
            method="POST",
            origin=True,
            content_type="application/json; charset=utf-8",
            body=body,
        )[0]
        == 200
    ), "a charset parameter is part of the media type, not a different one"


def test_an_oversized_command_is_refused_on_what_it_declares(client, repository, opportunity):
    """Refused before the body is read, so an enormous request costs nothing to refuse."""
    before = state(repository)
    padded = json.dumps(
        {"status": "applied", "expected_event_id": 1, "reason": "x" * (web.MAX_BODY + 1)}
    ).encode()
    assert len(padded) > web.MAX_BODY
    status, payload, _ = client.send(
        f"/api/v1/opportunities/{opportunity}/status",
        method="POST",
        origin=True,
        content_type="application/json",
        body=padded,
    )
    assert status == 413
    assert str(web.MAX_BODY) in json.loads(payload)["error"]
    assert state(repository) == before


def test_a_command_needs_a_length_it_can_be_held_to(client, repository, opportunity):
    """No declared length means nothing to bound, and this surface decodes no chunked body."""
    status, payload, _ = client.send(
        f"/api/v1/opportunities/{opportunity}/status",
        method="POST",
        origin=True,
        content_type="application/json",
        body=b'{"status": "applied", "expected_event_id": 1}',
        length="omit",
    )
    assert status == 400 and "Content-Length" in json.loads(payload)["error"]


def test_a_command_must_carry_this_surfaces_own_origin(client, repository, opportunity):
    """Required, not merely checked when present.

    A read with no Origin is an ordinary same-document fetch. A write with none has nothing
    to say for where it came from, and this is the request that changes the record.
    """
    event = repository.opportunity(opportunity)["status_event"]
    before = state(repository)
    for origin in (False, "http://evil.example.com", "null", f"https://{surface_authority()}"):
        status, body = client.command(
            opportunity, {"status": "applied", "expected_event_id": event}, origin=origin
        )
        assert status == 403, origin
        assert "Origin" in body["error"], origin
    assert state(repository) == before


def surface_authority():
    """Named so the parametrised origins above read as what they are."""
    return "127.0.0.1:1"


def test_a_command_without_the_launch_token_is_refused_before_its_body_is_read(
    client, repository, opportunity
):
    event = repository.opportunity(opportunity)["status_event"]
    before = state(repository)
    for token in (False, "", "wrong"):
        status, body = client.command(
            opportunity, {"status": "applied", "expected_event_id": event}, token=token
        )
        assert status == 401, token
        assert body == {"error": "A valid launch token is required"}
    assert state(repository) == before


def test_a_command_naming_an_unknown_opportunity_is_not_found(client, repository):
    status, body = client.command(
        "no-such-opportunity", {"status": "applied", "expected_event_id": 1}
    )
    assert status == 404 and body == {"error": "No record with that id"}


def test_a_command_refuses_a_query_string(client, repository, opportunity):
    event = repository.opportunity(opportunity)["status_event"]
    status, payload, _ = client.send(
        f"/api/v1/opportunities/{opportunity}/status?force=true",
        method="POST",
        origin=True,
        content_type="application/json",
        body=json.dumps({"status": "applied", "expected_event_id": event}).encode(),
    )
    assert status == 400 and "Unknown parameter" in json.loads(payload)["error"]


def test_the_command_refuses_before_reading_the_body_in_the_order_that_matters(
    client, repository, opportunity
):
    """Host, then Origin, then token -- each settled before a byte of the body is parsed.

    Proved by sending a body that would itself be a 400: whichever provenance check is
    reached first must answer instead, which is only true if the body is never looked at.
    """
    nonsense = b"{not json at all"
    address = f"/api/v1/opportunities/{opportunity}/status"
    common = {"method": "POST", "content_type": "application/json", "body": nonsense}
    assert client.send(address, host="evil.example.com", origin=True, **common)[0] == 403
    assert client.send(address, origin=False, **common)[0] == 403
    assert client.send(address, origin=True, token=False, **common)[0] == 401
    # With provenance in order, the same body is finally read -- and refused.
    assert client.send(address, origin=True, **common)[0] == 400


def test_the_status_vocabulary_is_served_rather_than_copied(client, repository):
    """The browser fills its control from this, so there is no second copy to drift."""
    status, body = client.json("/api/v1/statuses")
    assert status == 200
    assert body == {"statuses": list(STATUSES)}
    assert body["statuses"][0] == "new", "the module's order is the order served"
    assert client.json("/api/v1/statuses?limit=1")[0] == 400


def test_recording_a_status_is_visible_in_every_projection_that_reports_it(
    client, repository, opportunity
):
    """What the browser re-reads after a command is the engine's answer, not a local patch."""
    event = repository.opportunity(opportunity)["status_event"]
    assert (
        client.command(opportunity, {"status": "withdrawn", "expected_event_id": event})[0] == 200
    )

    detail = client.json(f"/api/v1/opportunities/{opportunity}?presentation=true")[1]
    assert detail["status"] == "withdrawn"
    assert detail["status_event"] != event
    listed = next(
        row
        for row in client.json("/api/v1/opportunities?presentation=true")[1]
        if row["id"] == opportunity
    )
    assert listed["status"] == "withdrawn"
    assert listed["presentation"] == detail["presentation"], "list and detail disagree"
    newest = client.json("/api/v1/timeline?limit=1")[1][0]
    assert (newest["kind"], newest["event"], newest["actor"]) == ("status", "withdrawn", "operator")


# --- under load -----------------------------------------------------------------------------------


def test_concurrent_reads_all_succeed_without_sharing_a_connection(client, repository, surface):
    """Threaded on purpose, and no sqlite3 object crosses a thread to make it work.

    Repository holds a path; each of its methods opens and closes its own connection on the
    calling thread. That is why the handle can be shared here, and why this package creates
    no pool and never weakens check_same_thread -- either of which would turn a slow pane
    into a data race.
    """
    opportunity = repository.opportunities()[0]["id"]
    paths = (
        "/api/v1/session",
        "/api/v1/opportunities",
        f"/api/v1/opportunities/{opportunity}",
        "/api/v1/communications",
        "/api/v1/timeline",
    )
    assert surface.daemon_threads is True
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda path: client.send(path)[0], paths * 8))
    assert results == [200] * len(results)
    assert not [node for name, node in nodes(ast.keyword) if node.arg == "check_same_thread"]


# --- serving --------------------------------------------------------------------------------------


def test_serving_prints_the_address_and_opens_no_browser(repository, monkeypatch):
    """The URL is printed. Opening a browser is convenience and is not this change."""
    monkeypatch.setattr(
        web.Surface, "serve_forever", lambda self: (_ for _ in ()).throw(KeyboardInterrupt)
    )
    said = []
    web.serve(repository, port=0, announce=said.append)
    spoken = "\n".join(said)
    assert "#token=" in spoken and str(repository.path) in spoken
    assert web.LOOPBACK in spoken and "0.0.0.0" not in spoken
    assert "webbrowser" not in imports(), "the surface opens a browser as well as printing"


# --- the guard that outlives the tests above ------------------------------------------------------

# Every repository member this package is allowed to touch. All of them read.
PERMITTED_READS = frozenset(
    {
        "opportunities",
        "opportunity",
        "sources",
        "communications",
        "communication",
        "review",
        "authorization",
        "timeline",
        "path",
    }
)
# The repository mutations this package may reach. Everything an operator can change from
# the browser goes through one of these, so the list of what the browser can do to the
# database is this line. It grew from {"record_status"} in #31 to admit `decide` and
# nothing else: creating a draft, reconciling one and every provider call stay below.
PERMITTED_WRITES = frozenset({"record_status", "decide"})
# Names that decide, contact a provider, or carry a credential -- and every other mutation.
# None may appear anywhere in this package, as a call, an attribute or an import.
#
# `draft` and `reconcile` left this set in PR 8, and nothing else did. What that admits is
# narrow and worth stating exactly: this package may now *invoke* the two outward commands on
# a service it was handed. It still may not name `OutwardActions`, so it cannot construct one;
# it still may not name `claim`, `finish`, `refuse` or `reject`, so it cannot reimplement what
# the service does; and it still may not name a provider or a credential class, so it cannot
# reach a mailbox except through the service that owns that authority. The capability the
# browser gained is the service's, exercised, not the workflow's, copied.
FORBIDDEN_NAMES = frozenset(
    {
        "Intake",
        "OutwardActions",
        "GmailDrafts",
        "GmailReader",
        "GmailCredentials",
        "GmailComposeCredentials",
        "ControlledDrafts",
        "claim",
        "finish",
        "refuse",
        "reject",
        "ingest",
        "intake",
        "intake_message",
        "migrate",
        "execute",
        "executemany",
        "commit",
        "transaction",
        "connection",
    }
)


def test_the_package_reaches_exactly_one_write_and_no_second_business_layer():
    """Read mechanically, because a passing response body cannot show this.

    A route that started deciding something of its own would answer 200 exactly as before.
    What says otherwise is the source: this package may name the repository's read
    projections and the mutations it is permitted, and may not name a provider or a
    credential at all -- so a future edit that reaches one fails here rather than at review.
    """
    allowed = PERMITTED_READS | PERMITTED_WRITES
    for name, tree in package_modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in FORBIDDEN_NAMES, f"{name}: .{node.attr}"
                if isinstance(node.value, ast.Name) and node.value.id == "repository":
                    assert node.attr in allowed, f"{name}: repository.{node.attr}"
            if isinstance(node, ast.Name):
                assert node.id not in FORBIDDEN_NAMES, f"{name}: {node.id}"


def test_the_only_mutations_the_package_reaches_are_the_append_and_the_decision():
    """Stated positively, so the guard says what the browser can do rather than only what
    it cannot. Every other way to change the database is in FORBIDDEN_NAMES above; this
    asserts the ones that are left are the ones the contract names, and that both are used.

    Written as an equality against a literal rather than against PERMITTED_WRITES alone, so
    widening the capability means editing this line too. A guard that reads its own
    expectation from the same constant the code was changed to satisfy would let the next
    mutation in quietly.
    """
    reached = {
        node.attr
        for name, node in nodes(ast.Attribute)
        if _named(node.value, "repository") and node.attr not in PERMITTED_READS
    }
    assert reached == PERMITTED_WRITES == {"record_status", "decide"}, reached


def test_the_package_imports_no_behaviour_from_the_communications_layer():
    """Constants only.

    The session facts report whether a Gmail credential is configured, which needs the two
    variable names and nothing else. Importing a reader or a draft provider to answer that
    would put a network client one attribute away from a request handler.
    """
    for name, tree in package_modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "communications"
            ):
                for alias in node.names:
                    assert alias.name.isupper(), f"{name}: from {node.module} import {alias.name}"
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith("communications"), (
                        f"{name}: import {alias.name}"
                    )


def test_the_package_declares_every_route_in_one_place():
    """The reads are declared in one match statement, and the commands in another.

    Counting the reads here is what makes that structural rather than aspirational: a route
    added anywhere else would leave this number behind. The commands are counted where they
    are routed, so adding one means writing it beside the others rather than registering it
    somewhere a reader would not think to look.
    """
    tree = dict(package_modules())["server.py"]
    projection = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_projection"
    )
    matches = [node for node in ast.walk(projection) if isinstance(node, ast.Match)]
    assert len(matches) == 1
    assert len(matches[0].cases) == 9


# --- the serve command ------------------------------------------------------------------------


def invoke(monkeypatch, *arguments):
    """Run `careersignal ...` with the surface itself replaced by a recorder.

    The command's job is to hand the right database and port to the surface and then block;
    what the surface does with them is the rest of this file.
    """
    served = []
    monkeypatch.setattr(
        cli,
        "serve",
        lambda repository, *, port, provider, provider_namespace, actions: served.append(
            (repository, port, provider, provider_namespace, actions)
        ),
    )
    monkeypatch.setattr("sys.argv", ["careersignal", *arguments])
    cli.main()
    return served


def test_the_command_serves_the_named_database_on_the_named_port(monkeypatch, capsys, tmp_path):
    served = invoke(monkeypatch, "serve", "--db", str(tmp_path / "db"), "--port", "9999")
    assert len(served) == 1
    repository, port, provider, namespace, _ = served[0]
    assert (repository.path, port) == (store.database_path(tmp_path / "db"), 9999)
    # The destination an approval would name, defaulted rather than inferred: the external
    # provider is never chosen implicitly, here or anywhere else.
    assert (provider, namespace) == ("controlled", "controlled")
    # No JSON report: this command has no operator decision in it and blocks rather than
    # returning an outcome, so it must not fall through to the pipeline commands' print.
    assert capsys.readouterr().out == ""


def test_the_command_uses_the_documented_default_port(monkeypatch, capsys, tmp_path):
    served = invoke(monkeypatch, "serve", "--db", str(tmp_path / "db"))
    assert served[0][1] == web.DEFAULT_PORT == 8765
    capsys.readouterr()


def test_the_command_records_nothing(monkeypatch, capsys, repository):
    before = state(repository)
    invoke(monkeypatch, "serve", "--db", str(repository.path))
    assert state(repository) == before
    capsys.readouterr()


def test_the_local_provider_gives_the_surface_outward_authority(monkeypatch, capsys, tmp_path):
    """The controlled provider stays on this machine, so there is nothing to be without."""
    served = invoke(monkeypatch, "serve", "--db", str(tmp_path / "db"))
    assert served[0][4] is not None
    capsys.readouterr()


def test_gmail_without_a_compose_credential_still_serves_and_cannot_draft(
    monkeypatch, capsys, tmp_path
):
    """The pairing PR 7 established, kept intact now that drafting is reachable from here.

    An operator serving the UI to record decisions has not asked to create anything. Refusing
    to start without a compose credential would make the safe half of the workflow depend on
    the unsafe half -- the dependency that was deliberately removed -- and an approval still
    only names where a draft may go.
    """
    monkeypatch.delenv(COMPOSE_TOKEN_VARIABLE, raising=False)
    served = invoke(
        monkeypatch,
        "serve",
        "--db",
        str(tmp_path / "db"),
        "--provider",
        "gmail",
        "--mailbox",
        "operator@example.com",
    )
    repository, _, provider, namespace, actions = served[0]
    # The destination is still declared, so approvals recorded here still name it.
    assert (provider, namespace) == TARGET
    # And there is no service behind them, so nothing here can reach that mailbox.
    assert actions is None
    capsys.readouterr()


def test_gmail_with_a_compose_credential_gives_the_surface_outward_authority(
    monkeypatch, capsys, tmp_path
):
    monkeypatch.setenv(COMPOSE_TOKEN_VARIABLE, "ya29-not-a-real-compose-credential")
    served = invoke(
        monkeypatch,
        "serve",
        "--db",
        str(tmp_path / "db"),
        "--provider",
        "gmail",
        "--mailbox",
        "operator@example.com",
    )
    assert served[0][4] is not None
    capsys.readouterr()


# --- the packet-bound decision ------------------------------------------------------------------


TARGET = ("gmail", "gmail:operator@example.com")


@pytest.fixture
def addressed(repository):
    """A surface launched with a declared Gmail destination and no compose credential.

    The pairing is the point. Approving names where a draft may go; it has never needed the
    ability to reach it, and a surface that could only record approvals once a credential
    was configured would make the safe half of the workflow depend on the unsafe half.
    """
    running = web.Surface(repository, port=0, provider=TARGET[0], provider_namespace=TARGET[1])
    thread = threading.Thread(target=running.serve_forever, kwargs={"poll_interval": 0.02})
    thread.daemon = True
    thread.start()
    try:
        yield running
    finally:
        running.shutdown()
        running.server_close()
        thread.join(timeout=10)


def review_of(repository, opportunity):
    return repository.opportunity(opportunity)["review"]


def packet(repository, opportunity) -> dict:
    """The expectation exactly as the detail read reports it, which is what a browser holds."""
    return repository.opportunity(opportunity)["bound"]["expected"]


def decisions(repository) -> dict:
    """Every decision and every audit event, so a refusal that wrote something is visible."""
    return {
        "actions": {
            row["id"]: repository.opportunity(row["id"])["action"]
            for row in repository.opportunities()
        },
        "audit": [event for event in repository.timeline() if event["kind"] == "audit"],
    }


def decide(client, review, payload, **kwargs):
    settings = {
        "method": "POST",
        "origin": True,
        "content_type": "application/json",
        "body": json.dumps(payload).encode("utf-8"),
    } | kwargs
    status, body, _ = client.send(f"/api/v1/reviews/{part(review)}/decision", **settings)
    return status, json.loads(body)


def part(value) -> str:
    return quote(str(value), safe="")


def test_an_approval_against_the_visible_packet_is_recorded_with_the_launch_destination(
    addressed, repository, opportunity
):
    """The straightforward case, and the one every refusal below is measured against."""
    client = Client(addressed)
    review = review_of(repository, opportunity)
    status, body = decide(
        client, review, {"approved": True, "expected": packet(repository, opportunity)}
    )
    assert status == 200
    assert body == {
        "review": review,
        "approved": True,
        "actor": "operator",
        "provider": TARGET[0],
        "provider_namespace": TARGET[1],
    }
    action = repository.opportunity(opportunity)["action"]
    assert (action["decision"], action["actor"], action["binds"]) == ("approved", "operator", True)
    # The destination came from the launch, not from anything the caller could name.
    assert (action["provider"], action["provider_namespace"]) == TARGET


def test_a_later_message_between_reading_the_packet_and_approving_it_refuses_the_approval(
    addressed, repository, opportunity
):
    """The race this whole design exists to close.

    The operator reads a packet, a message arrives that moves where a draft would be
    addressed, and the approval they then record would bind an address nobody ever saw --
    while every digest they *did* see still matched. The expectation they hand back is what
    makes that detectable, and `decide()` compares it inside the write transaction.
    """
    client = Client(addressed)
    review = review_of(repository, opportunity)
    held = packet(repository, opportunity)
    before = decisions(repository)

    Intake(repository, Profile(("python", "sql"))).intake_message(
        alert(sender="someone.else@example.net", external_id="m2")
    )
    assert packet(repository, opportunity)["addressing_digest"] != held["addressing_digest"]

    status, body = decide(client, review, {"approved": True, "expected": held})
    assert status == 409
    assert body["error"] == "binding_conflict"
    # What moved, named: "something changed" is not enough to decide against.
    moved = [
        field for field in body["observed"] if body["expected"][field] != body["observed"][field]
    ]
    assert moved == ["addressing_digest"]
    # Nothing was written -- no decision row and no audit event.
    assert decisions(repository) == before


def test_a_status_event_between_reading_the_packet_and_approving_it_refuses_the_approval(
    addressed, repository, opportunity
):
    """The same rule, moved by the other ledger.

    An approval binds the status event it was read against, so an opportunity the operator
    has since moved is not one they can approve from the screen that predates the move.
    """
    client = Client(addressed)
    review = review_of(repository, opportunity)
    held = packet(repository, opportunity)
    before = decisions(repository)

    repository.record_status(opportunity, "interested", actor="operator", reason="")
    status, body = decide(client, review, {"approved": True, "expected": held})
    assert status == 409 and body["error"] == "binding_conflict"
    moved = [
        field for field in body["observed"] if body["expected"][field] != body["observed"][field]
    ]
    assert moved == ["status_event_id"]
    assert body["observed"]["status_event_id"] == held["status_event_id"] + 1
    assert decisions(repository) == before


def test_a_rejection_needs_no_packet_even_after_the_packet_has_moved(
    addressed, repository, opportunity
):
    """The asymmetry, stated as behaviour.

    A rejection binds nothing and authorizes nothing. Demanding a fresh packet to record
    one would put the safest action an operator can take behind the same precondition as
    the riskiest -- which is backwards where it matters most.
    """
    client = Client(addressed)
    review = review_of(repository, opportunity)
    Intake(repository, Profile(("python", "sql"))).intake_message(
        alert(sender="someone.else@example.net", external_id="m2")
    )
    status, body = decide(client, review, {"approved": False})
    assert status == 200
    assert body == {"review": review, "approved": False, "actor": "operator"}
    # No destination is reported, because a rejection authorizes none.
    assert "provider" not in body
    assert repository.opportunity(opportunity)["action"]["decision"] == "rejected"


def test_an_approval_without_its_expectation_is_refused(addressed, repository, opportunity):
    """Not defaulted to "whatever is current": that is the unbound write this route excludes."""
    client = Client(addressed)
    before = decisions(repository)
    status, body = decide(client, review_of(repository, opportunity), {"approved": True})
    assert status == 400
    assert "expectation" in body["error"]
    assert decisions(repository) == before


def test_a_rejection_carrying_an_expectation_is_refused_rather_than_ignored(
    addressed, repository, opportunity
):
    """Refused, because a caller that sent one believed it was being honoured.

    Silently dropping it would record a decision on terms the caller did not ask for, and
    the caller would have no way to discover that its precondition was never applied.
    """
    client = Client(addressed)
    before = decisions(repository)
    status, body = decide(
        client,
        review_of(repository, opportunity),
        {"approved": False, "expected": packet(repository, opportunity)},
    )
    assert status == 400
    assert "binds nothing" in body["error"]
    assert decisions(repository) == before


@pytest.mark.parametrize(
    "extra",
    (
        {"actor": "somebody else"},
        {"provider": "gmail"},
        {"provider_namespace": "gmail:attacker@example.net"},
        {"mailbox": "attacker@example.net"},
    ),
)
def test_the_browser_cannot_name_the_actor_or_the_destination(
    addressed, repository, opportunity, extra
):
    """Refused as unknown fields, so none of them can be quietly honoured or quietly dropped.

    The destination is a launch fact for the same reason the actor is: a page that could
    name where an approval points could point it somewhere the operator never chose.
    """
    client = Client(addressed)
    before = decisions(repository)
    status, body = decide(
        client,
        review_of(repository, opportunity),
        {"approved": True, "expected": packet(repository, opportunity), **extra},
    )
    assert status == 400
    assert body["error"].startswith("Unknown field")
    assert decisions(repository) == before


def test_an_approval_is_recorded_with_no_compose_credential_in_the_environment(
    addressed, repository, opportunity, monkeypatch
):
    """Declaring a destination is not the same as being able to reach it.

    `careersignal serve --provider gmail --mailbox ...` names where an approval points.
    Requiring a working compose token to record one would tie the operator's decision to
    whether a network client could be constructed, which approving has never needed.
    """
    monkeypatch.delenv(COMPOSE_TOKEN_VARIABLE, raising=False)
    client = Client(addressed)
    status, body = client.json("/api/v1/session")
    assert status == 200
    assert body["gmail_compose_token"] is False
    assert body["decision_target"] == {"provider": TARGET[0], "provider_namespace": TARGET[1]}
    status, _ = decide(
        client,
        review_of(repository, opportunity),
        {"approved": True, "expected": packet(repository, opportunity)},
    )
    assert status == 200
    assert repository.opportunity(opportunity)["action"]["provider_namespace"] == TARGET[1]


def test_a_decision_locked_behind_a_draft_intent_is_a_conflict_not_a_bad_request(
    addressed, repository, opportunity
):
    """A forged POST reaches the same refusal the screen declines to offer.

    The browser shows no decision control once a draft has been attempted, but the control
    is presentation: what actually holds the line is `decide()`, and a request that skips
    the page entirely still gets the state's answer rather than a write.
    """
    client = Client(addressed)
    review = review_of(repository, opportunity)
    assert (
        decide(client, review, {"approved": True, "expected": packet(repository, opportunity)})[0]
        == 200
    )
    # Claimed for the destination the approval actually names, so the intent exists for
    # the right reason rather than because the provider disagreed.
    repository.claim(review, provider=TARGET[0], provider_namespace=TARGET[1])
    before = decisions(repository)
    status, body = decide(
        client, review, {"approved": True, "expected": packet(repository, opportunity)}
    )
    # A well-formed request the state refuses: not a malformed body, and not a moved packet.
    assert status == 409
    assert body["error"] == "decision_refused"
    assert "reconciliation" in body["detail"]
    assert decisions(repository) == before


def test_the_decision_address_is_as_singular_as_the_status_address(
    addressed, repository, opportunity
):
    """The path rules established in #31 are the router's, not one route's."""
    client = Client(addressed)
    review = review_of(repository, opportunity)
    payload = json.dumps({"approved": True, "expected": packet(repository, opportunity)}).encode(
        "utf-8"
    )
    before = decisions(repository)
    for path in (
        f"/abcdef/reviews/{part(review)}/decision",
        f"/api/v2/reviews/{part(review)}/decision",
        f"/api/v1//reviews/{part(review)}/decision",
        f"/api/v1/reviews//{part(review)}/decision",
        f"/api/v1/reviews/{part(review)}/decision/",
        f"/api/v1/reviews/{part(review)}/decisions",
    ):
        assert client.unread(
            path, method="POST", origin=True, content_type="application/json", body=payload
        ) in (405, None), path
    assert client.unread(
        f"http://{addressed.authority}/api/v1/reviews/{part(review)}/decision",
        method="POST",
        origin=True,
        content_type="application/json",
        body=payload,
    ) in (400, None)
    assert decisions(repository) == before


def test_a_decision_needs_the_same_provenance_every_command_needs(
    addressed, repository, opportunity
):
    """Host, Origin and token, settled before the body is read, exactly as for a status."""
    client = Client(addressed)
    review = review_of(repository, opportunity)
    body = json.dumps({"approved": True, "expected": packet(repository, opportunity)}).encode()
    before = decisions(repository)
    sent = {"method": "POST", "content_type": "application/json", "body": body}
    assert client.unread(
        f"/api/v1/reviews/{part(review)}/decision", origin=True, token=False, **sent
    ) in (401, None)
    assert client.unread(f"/api/v1/reviews/{part(review)}/decision", **sent) in (403, None)
    assert client.unread(
        f"/api/v1/reviews/{part(review)}/decision", origin="http://evil.example.com", **sent
    ) in (403, None)
    assert client.unread(
        f"/api/v1/reviews/{part(review)}/decision", origin=True, host="evil.example.com", **sent
    ) in (403, None)
    assert decisions(repository) == before


def test_an_unknown_review_is_a_404_before_the_repository_is_asked_to_decide(addressed, repository):
    """Absence and staleness stay distinguishable, the same rule the read routes follow."""
    client = Client(addressed)
    status, body = decide(client, "no-such-review", {"approved": False})
    assert status == 404
    assert body == {"error": "No record with that id"}


def test_an_oversized_decision_is_refused_on_what_it_declares(addressed, repository, opportunity):
    client = Client(addressed)
    status, body, _ = client.send(
        f"/api/v1/reviews/{part(review_of(repository, opportunity))}/decision",
        method="POST",
        origin=True,
        content_type="application/json",
        body=b'{"approved": false}',
        length=web.MAX_BODY + 1,
    )
    assert status == 413
    assert str(web.MAX_BODY) in json.loads(body)["error"]


@pytest.mark.parametrize(
    "malformed",
    (
        {"approved": "yes", "expected": {}},
        {"approved": 1, "expected": {}},
        {"approved": None},
        {"expected": {}},
        {"approved": True, "expected": "digest"},
        {"approved": True, "expected": []},
        {"approved": True, "expected": {"content_digest": "a"}},
        {
            "approved": True,
            "expected": {
                "content_digest": "a",
                "draft_digest": "b",
                "addressing_digest": "c",
                "status_event_id": True,
            },
        },
        {
            "approved": True,
            "expected": {
                "content_digest": "a",
                "draft_digest": "b",
                "addressing_digest": "c",
                "status_event_id": "1",
            },
        },
        {
            "approved": True,
            "expected": {
                "content_digest": 1,
                "draft_digest": "b",
                "addressing_digest": "c",
                "status_event_id": 1,
            },
        },
        {
            "approved": True,
            "expected": {
                "content_digest": "a",
                "draft_digest": "b",
                "addressing_digest": "c",
                "status_event_id": 1,
                "extra": "x",
            },
        },
    ),
)
def test_a_malformed_decision_is_refused_before_anything_is_decided(
    addressed, repository, opportunity, malformed
):
    """Shape only. What the values *mean* is decide()'s, inside the transaction."""
    client = Client(addressed)
    before = decisions(repository)
    status, _ = decide(client, review_of(repository, opportunity), malformed)
    assert status == 400, malformed
    assert decisions(repository) == before


def test_the_expectation_the_packet_carries_is_the_one_the_binding_was_read_from(repository):
    """The projection change this PR turns on, asserted where it is made.

    `bound` and `expected` come from one `_binding()` snapshot. If `expected` were read
    again -- by a second query, or by a second HTTP call -- it could name a moment the
    packet beside it never showed, which is the window the expectation exists to close.
    """
    record = repository.opportunity(repository.opportunities()[0]["id"])
    bound = record["bound"]
    assert set(bound) == {"review", "source", "to", "subject", "wording", "expected"}
    assert set(bound["expected"]) == {
        "content_digest",
        "draft_digest",
        "addressing_digest",
        "status_event_id",
    }
    # The same four values decide() compares against, and the same four authorization()
    # reports as current -- because all three read one _binding(). Compared field by field
    # against that independent projection rather than against a literal, so a change that
    # made the packet's expectation drift from the engine's own current view fails here.
    current = repository.authorization(record["review"])
    assert bound["expected"] == {field: current[field] for field in bound["expected"]}
    assert bound["expected"]["status_event_id"] == record["status_event"]


# --- what `serve --help` tells an operator ------------------------------------------------------


def helped(monkeypatch, capsys, *arguments) -> str:
    """What argparse prints under `options:`, wrapping flattened so a claim reads whole.

    The usage line is dropped deliberately. It repeats every flag name, so slicing on one
    there finds a bracketed placeholder rather than the sentence being checked -- which is
    what the first version of this helper did.
    """
    monkeypatch.setattr("sys.argv", ["careersignal", *arguments])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 0
    printed = capsys.readouterr().out
    return " ".join(printed[printed.index("options:") :].split())


def flag(text, name, next_name) -> str:
    """One option's help, bounded by the option that follows it."""
    section = text[text.index(name) :]
    return section[: section.index(next_name)]


def test_serve_help_distinguishes_declaring_a_destination_from_writing_to_it(monkeypatch, capsys):
    """The one security-significant distinction in this help, pinned on purpose.

    `--provider gmail` means two different things at two different commands. At `draft` it
    creates a real draft in a real mailbox. At `serve` it only names the mailbox an approval
    would authorize -- nothing is sent, nothing is composed, and no compose credential is
    needed. An operator reading `serve --help` and concluding that launching the browser can
    create a draft would have exactly the wrong model of what they just started.

    Only this distinction is pinned, not the whole of argparse's output: a snapshot of every
    string would fail on an unrelated rewording and teach the next person to re-record it
    without reading it, which is how a guard stops being one.

    PR 8 will invalidate this deliberately, when `serve` gains outward draft authority. That
    is the point: the help and this test then have to change together, in that change, rather
    than leaving yesterday's claim behind for review to find.
    """
    provider = flag(helped(monkeypatch, capsys, "serve", "--help"), "--provider", "--message")
    # What it does at draft, and what it does not do here.
    assert "creates a real draft" in provider
    assert "for serve only names that mailbox" in provider
    assert "serve writes nothing to Gmail and needs no compose token" in provider
    # And the flag is never the default, at either command.
    assert "Never the default" in provider


def test_serve_help_says_where_an_approval_recorded_in_the_browser_would_point(monkeypatch, capsys):
    """`--mailbox` is what turns the declaration into a specific destination."""
    mailbox = flag(helped(monkeypatch, capsys, "serve", "--help"), "--mailbox", "--query")
    assert "approve/serve" in mailbox
    assert "where an approval says a draft may go" in mailbox
