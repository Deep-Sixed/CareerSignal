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
import sqlite3
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
from recruiting.ports import ProviderRejected
from recruiting.status import STATUSES
from system import cli, views
from system import outward as outward_module
from system.web import server as web
from system.workflow import Intake, OutwardActions

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
        # The outward commands answer POST and nothing else. Reading one would be a route
        # that reports whether a draft could be made, which is not a question this surface
        # answers anywhere but in the projection that already describes the review.
        "/api/v1/reviews/x/draft",
        "/api/v1/reviews/x/reconcile",
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


def router(tree):
    """The one match statement that dispatches a command address, or None.

    Identified by what it matches *on* -- `tail(parsed.path)` -- rather than by being a match
    inside `_command`, because "a match in that function" is not the same claim. A second
    match beside the router, or one nested inside a route case, is ordinary code that happens
    to sit in the same function, and a queue name in its patterns would be exactly the local
    interpretation this forbids. Exactly one is expected, and finding two is itself a failure:
    the address is dispatched in one place or the rule has stopped meaning anything.
    """
    routers = [
        node
        for command in ast.walk(tree)
        if isinstance(command, ast.FunctionDef | ast.AsyncFunctionDef)
        and command.name == "_command"
        for node in ast.walk(command)
        if isinstance(node, ast.Match)
        and isinstance(node.subject, ast.Call)
        and isinstance(node.subject.func, ast.Name)
        and node.subject.func.id == "tail"
    ]
    assert len(routers) <= 1, f"{len(routers)} command routers; the address is dispatched once"
    return routers[0] if routers else None


def routed_only(vocabulary):
    """Every string from `vocabulary` in this package must be a command route pattern.

    `draft` and `reconcile` name two outward commands and also two queues. The collision is
    real and cannot be spelled away, so it is resolved by where the string sits rather than by
    excusing the word: in a `case` pattern of the command router it is an address. Anywhere
    else -- a comparison, a lookup table, a response body -- it would be this layer forming an
    opinion about what a row means, which is the thing being forbidden.

    Three earlier versions were too wide, each in a way that looked right at the time. The
    first excused any module-level tuple of strings, which is most of the constants in that
    file. The second excused everything beneath any `ast.Match`, but a match *body* is
    ordinary code, so a queue name compared inside `_projection` would have been waved
    through. The third narrowed to `case` patterns under `_command` -- and still trusted
    *every* match in that function, so a second one beside the router, or one nested inside a
    route case, would have been read as routing.

    The whitelist is now the patterns of the router itself: not the guard, not the body, not a
    neighbouring match, not a nested one, and not another function's.
    """
    for name, tree in package_modules():
        dispatch = router(tree)
        routed = (
            {id(node) for case in dispatch.cases for node in ast.walk(case.pattern)}
            if dispatch
            else set()
        )
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in vocabulary:
                assert id(node) in routed, f"{name}: {node.value!r} outside a command route pattern"


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
# `draft` and `reconcile` left this set in PR 8, and `ingest_eml` and `ingest_gmail` never
# entered it in PR 9. What that admits is narrow and worth stating exactly: this package may
# now *invoke* four named commands on two services it was handed. It still may not name
# `OutwardActions` or `IntakeActions`, so it cannot construct either; it still may not name
# `claim`, `finish`, `refuse`, `reject`, `ingest`, `intake` or `intake_message`, so it cannot
# reimplement what they do; it still may not name a provider, a reader or a credential class,
# so it cannot reach a mailbox except through the service that owns that authority; and as of
# PR 9 it may not name `Message`, `Profile`, `evaluate` or `extract` either, so it can neither
# parse incoming material nor decide what the material is judged against. The capability the
# browser gained is the services', exercised, not the workflows', copied.
#
# `getattr` and its neighbours are here for a different reason, and it is the one that makes
# this whole guard mean anything. Every rule in this file reads the syntax tree. A command
# looked up by name -- `getattr(service, chosen)(...)` -- is an authority the tree cannot see,
# so a dynamic dispatcher would not be a way around one rule but a way around all of them.
FORBIDDEN_NAMES = frozenset(
    {
        "Intake",
        "IntakeActions",
        "OutwardActions",
        "GmailDrafts",
        "GmailReader",
        "GmailCredentials",
        "GmailComposeCredentials",
        "ControlledDrafts",
        "Message",
        "Profile",
        "claim",
        "finish",
        "refuse",
        "reject",
        "ingest",
        "intake",
        "intake_message",
        "from_bytes",
        "evaluate",
        "extract",
        "extract_records",
        "parse_message",
        "identifiers",
        "messages",
        "verify_identity",
        "migrate",
        "execute",
        "executemany",
        "commit",
        "transaction",
        "connection",
        "getattr",
        "setattr",
        "globals",
        "vars",
        "eval",
        "exec",
    }
)


def offending(name, tree) -> list:
    """Every forbidden name in one parsed module, and every repository member outside the list.

    Extracted so the guard below and the mutations that prove it bites read the same code. A
    mutation test that re-implemented the check would prove that its own copy fails, which is
    not the claim anybody wants made.

    Import lines are read too, not only uses. `from system.intake import IntakeActions` binds
    an alias rather than an `ast.Name`, so a scan of names alone would let the class into the
    module and only object once somebody called it -- and a class that is present is a class
    the next edit can reach for.
    """
    allowed = PERMITTED_READS | PERMITTED_WRITES
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom | ast.Import):
            for alias in node.names:
                if alias.name in FORBIDDEN_NAMES or (alias.asname or "") in FORBIDDEN_NAMES:
                    found.append(f"{name}: import {alias.name}")
        if isinstance(node, ast.Attribute):
            if node.attr in FORBIDDEN_NAMES:
                found.append(f"{name}: .{node.attr}")
            if isinstance(node.value, ast.Name) and node.value.id == "repository":
                if node.attr not in allowed:
                    found.append(f"{name}: repository.{node.attr}")
        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            found.append(f"{name}: {node.id}")
    return found


def test_the_package_reaches_exactly_one_write_and_no_second_business_layer():
    """Read mechanically, because a passing response body cannot show this.

    A route that started deciding something of its own would answer 200 exactly as before.
    What says otherwise is the source: this package may name the repository's read
    projections and the mutations it is permitted, and may not name a provider or a
    credential at all -- so a future edit that reaches one fails here rather than at review.
    """
    for name, tree in package_modules():
        assert not offending(name, tree), offending(name, tree)


@pytest.mark.parametrize(
    "mutation",
    [
        "def _eml(self):\n    return self.server.repository.ingest('m', [])\n",
        "def _eml(self, raw, ns):\n    return Intake(self.server.repository, p).intake_message(raw)\n",
        "from system.intake import IntakeActions\n",
        "from system.intake import IntakeActions as Taking\n",
        "from communications.message import Message\n",
        "from communications.gmail import GmailReader\n",
        "from recruiting.models import Profile\n",
        "def _parse(self, raw, ns):\n    return Message.from_bytes(raw, namespace=ns)\n",
        "def _profile(self, supplied):\n    return Profile(tuple(supplied['skills']))\n",
        "def _read(self):\n    return self.server.reader.messages(limit=100)\n",
        "def _run(self, name, raw, ns):\n    return getattr(self.server.inbound, name)(raw, ns)\n",
        "def _run(self, name):\n    return vars(self.server.inbound)[name]()\n",
        "def _run(self, source):\n    return eval('self.server.inbound.ingest_' + source)\n",
    ],
    ids=[
        "a direct repository ingest",
        "the workflow reimplemented",
        "the service imported",
        "the service imported under another name",
        "a message parser imported",
        "a mailbox reader imported",
        "an evaluation profile imported",
        "material parsed here",
        "a profile built from a request",
        "a mailbox read here",
        "a dynamic dispatcher",
        "a dispatcher through the instance dictionary",
        "a dispatcher through eval",
    ],
)
def test_the_guard_bites_on_every_way_this_package_could_take_material_in_itself(mutation):
    """The guard proved against the edits it exists to stop, not only against today's file.

    A guard that passes is evidence of nothing until it has been shown to fail. Every line
    here is a plausible next edit -- most of them would work perfectly at runtime -- and each
    is rejected by the same `offending()` the real check runs, on a module parsed the same way.

    The three dispatchers are the ones worth naming. `getattr`, the instance dictionary and
    `eval` all reach a command by a string, which is an authority the syntax tree cannot see:
    they would defeat not this rule but every rule in this file at once, which is why the
    names are forbidden rather than the shapes detected.
    """
    assert offending("mutant.py", ast.parse(mutation)), mutation


def test_the_guard_admits_what_the_surface_actually_does():
    """The other half of the pair, so the rule is a discrimination and not a blanket refusal.

    The two intake commands, invoked on the injected service, are exactly what PR 9 grants and
    must pass cleanly -- a guard that rejected them too would be satisfied by a package that
    had stopped working.
    """
    permitted = (
        "def _eml(self, raw, ns):\n    return self.server.inbound.ingest_eml(raw, ns)\n"
        "\n"
        "def _gmail(self, supplied):\n"
        "    return self.server.inbound.ingest_gmail(query=supplied['query'])\n"
        "\n"
        "def _sources(self):\n    return self.server.inbound.available()\n"
    )
    assert not offending("permitted.py", ast.parse(permitted))


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
    assert len(matches[0].cases) == 10
    # The commands are counted where they are routed, on the one match `router()` identifies by
    # what it dispatches on. Counting them makes a command added elsewhere -- a second match, a
    # registration, a lookup -- leave this number behind rather than pass unnoticed.
    assert len(router(tree).cases) == 6


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
        lambda repository, *, port, provider, provider_namespace, actions, inbound: served.append(
            (repository, port, provider, provider_namespace, actions, inbound)
        ),
    )
    monkeypatch.setattr("sys.argv", ["careersignal", *arguments])
    cli.main()
    return served


def test_the_command_serves_the_named_database_on_the_named_port(monkeypatch, capsys, tmp_path):
    served = invoke(monkeypatch, "serve", "--db", str(tmp_path / "db"), "--port", "9999")
    assert len(served) == 1
    repository, port, provider, namespace, _, _ = served[0]
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
    repository, _, provider, namespace, actions, _ = served[0]
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


# --- the outward commands -------------------------------------------------------------------------
#
# The safety property these are judged by: after an uncertain provider contact there is no
# route from this surface -- automatic or operator-triggered -- that creates a second draft
# until reconciliation has established what happened. Everything else here is the authority
# that has to hold for that property to mean anything.


class Outward:
    """A provider with each outcome a real one can produce, counting what it was asked to do.

    The counts are the point of the double rather than a convenience. "Nothing was created"
    is not observable from a response body -- a refusal and a rejection say much the same
    thing to a reader -- so every test below that claims nothing reached the provider proves
    it by the number of create calls, which is a fact about this object and not about wording.
    """

    provider, namespace = TARGET

    def __init__(self, *, create=None, found=None, identity=None):
        self.creates, self.lookups, self.identities = 0, 0, 0
        self._create, self._found, self._identity = create, found, identity
        self._lock = threading.Lock()

    def refusal(self, key, body, *, to="", subject_line=""):
        return None

    def identity(self):
        with self._lock:
            self.identities += 1
        if self._identity is not None:
            raise self._identity()
        return self.namespace

    def create(self, key, body, *, to="", subject_line=""):
        with self._lock:
            self.creates += 1
        if self._create is not None:
            raise self._create(key)
        return f"gmail-draft:{key}"

    def lookup(self, key):
        with self._lock:
            self.lookups += 1
        return self._found


@pytest.fixture
def acting(repository):
    """A surface that can reach a provider, and the provider it reaches.

    Built exactly as `serve` builds it: the service is constructed outside the web package
    and handed in, so nothing in `system.web` imports an adapter or holds a credential.
    """

    def launch(provider):
        running = web.Surface(
            repository,
            port=0,
            provider=TARGET[0],
            provider_namespace=TARGET[1],
            actions=OutwardActions(repository, provider),
        )
        thread = threading.Thread(target=running.serve_forever, kwargs={"poll_interval": 0.02})
        thread.daemon = True
        thread.start()
        launch.running.append((running, thread))
        return Client(running)

    launch.running = []
    try:
        yield launch
    finally:
        for running, thread in launch.running:
            running.shutdown()
            running.server_close()
            thread.join(timeout=10)


def outward(client, review, command, **kwargs):
    settings = {
        "method": "POST",
        "origin": True,
        "content_type": "application/json",
        "body": b"{}",
    } | kwargs
    status, body, _ = client.send(f"/api/v1/reviews/{part(review)}/{command}", **settings)
    return status, json.loads(body)


def approve(repository, opportunity):
    """An approval bound to the launch destination, recorded the way the surface records one."""
    review = review_of(repository, opportunity)
    repository.decide(
        review,
        approved=True,
        actor="operator",
        provider=TARGET[0],
        provider_namespace=TARGET[1],
        expected=packet(repository, opportunity),
    )
    return review


# --- draft authority ------------------------------------------------------------------------------


def test_an_approved_and_binding_packet_can_be_drafted(acting, repository, opportunity):
    """The straightforward case, and the one every refusal below is measured against."""
    provider = Outward()
    client = acting(provider)
    review = approve(repository, opportunity)

    status, body = outward(client, review, "draft")
    assert status == 200
    assert body["outcome"] == "accepted"
    assert body["receipt"] == f"gmail-draft:{review}"
    assert provider.creates == 1
    # And the receipt is durable, not merely reported.
    assert repository.intent(review) == ("confirmed", f"gmail-draft:{review}")


@pytest.mark.parametrize(
    ("situation", "arrange"),
    [
        ("never decided", lambda repository, opportunity: review_of(repository, opportunity)),
        ("rejected", lambda repository, opportunity: _rejected(repository, opportunity)),
        (
            "approval no longer binds",
            lambda repository, opportunity: _stale(repository, opportunity),
        ),
        (
            "opportunity is terminal",
            lambda repository, opportunity: _terminal(repository, opportunity),
        ),
    ],
)
def test_a_draft_without_current_authority_contacts_nothing(
    acting, repository, opportunity, situation, arrange
):
    """Four ways to lack authority, and the one thing that must be true of all of them.

    The outcome is a refusal and the provider was never asked to create anything -- proved by
    the count, not by the wording. A refusal that had already contacted a mailbox would be
    the uncertain case wearing a certain answer's name, and the operator would be told to
    correct and retry something that might already exist.
    """
    provider = Outward()
    client = acting(provider)
    review = arrange(repository, opportunity)

    status, body = outward(client, review, "draft")
    assert status == 409, situation
    assert body["outcome"] == "refused", situation
    assert provider.creates == 0, situation
    assert repository.intent(review) is None, situation


def _rejected(repository, opportunity):
    review = review_of(repository, opportunity)
    repository.decide(review, approved=False, actor="operator")
    return review


def _stale(repository, opportunity):
    review = approve(repository, opportunity)
    # A later message moves where a draft would be addressed, so the approval on record no
    # longer binds the material a draft would now compose.
    Intake(repository, Profile(("python", "sql"))).intake_message(
        alert(sender="someone.else@example.net", external_id="m2")
    )
    assert repository.opportunity(opportunity)["action"]["binds"] is False
    return review


def _terminal(repository, opportunity):
    review = approve(repository, opportunity)
    current = repository.opportunity(opportunity)["status_event"]
    repository.record_status(
        opportunity, "REJECTED", actor="operator", expected_event_id=current, reason=""
    )
    return review


def test_an_approval_for_another_destination_does_not_authorize_this_one(
    acting, repository, opportunity
):
    """An approval names where a draft may go, and a different launch is a different where."""
    review = review_of(repository, opportunity)
    repository.decide(
        review,
        approved=True,
        actor="operator",
        provider="gmail",
        provider_namespace="gmail:somebody.else@example.com",
        expected=packet(repository, opportunity),
    )
    provider = Outward()
    client = acting(provider)

    status, body = outward(client, review, "draft")
    assert status == 409
    assert body["outcome"] == "refused"
    assert provider.creates == 0


def test_a_review_that_names_nothing_is_not_found(acting, repository):
    """Absence is answered before authority, so a mistyped id is not reported as a refusal."""
    provider = Outward()
    status, _ = outward(acting(provider), "no-such-review", "draft")
    assert status == 404
    assert provider.creates == 0


# --- the four outcomes ----------------------------------------------------------------------------


def test_a_provider_that_proves_it_created_nothing_is_not_uncertain(
    acting, repository, opportunity
):
    """PROVIDER_REJECTED: contacted, and its own answer is evidence nothing exists.

    Distinct from REFUSED because a request was sent, and distinct from UNCERTAIN because the
    answer proves the outcome. Retrying once the cause is fixed is safe, and the record says
    so -- which is exactly what must not be said after an uncertain contact.
    """
    provider = Outward(create=lambda key: ProviderRejected("the mailbox declined the request"))
    client = acting(provider)
    review = approve(repository, opportunity)

    status, body = outward(client, review, "draft")
    assert status == 200
    assert body["outcome"] == "provider_rejected"
    assert provider.creates == 1
    # The intent this attempt reserved was released, so a corrected draft may claim again.
    assert repository.intent(review) is None
    # And the approval is untouched by the provider's refusal.
    assert repository.opportunity(opportunity)["action"]["decision"] == "approved"


def test_a_provider_whose_answer_never_came_back_is_uncertain(acting, repository, opportunity):
    """UNCERTAIN: contacted, and nothing about the response proves what happened."""
    provider = Outward(create=lambda key: OSError("the connection was reset"))
    client = acting(provider)
    review = approve(repository, opportunity)

    status, body = outward(client, review, "draft")
    assert status == 200
    assert body["outcome"] == "uncertain"
    assert provider.creates == 1
    assert repository.intent(review) == ("uncertain", None)


UNDECIDED = "the review was never approved"


@pytest.mark.parametrize(
    ("expected", "behaviour", "situation", "points_to_reconcile", "may_draft_again"),
    [
        ("accepted", None, None, False, False),
        ("provider_rejected", lambda key: ProviderRejected("declined"), None, False, True),
        ("uncertain", lambda key: OSError("reset"), None, True, False),
        ("refused", None, UNDECIDED, False, False),
    ],
)
def test_each_outcome_says_something_different_about_the_mailbox(
    acting,
    repository,
    opportunity,
    expected,
    behaviour,
    situation,
    points_to_reconcile,
    may_draft_again,
):
    """Four answers, four different things to do next. The risk here is collapse.

    Each of these is reachable from this surface and each says something different about
    whether a provider artifact might exist. A surface that reported them as success and
    failure would be correct about the request and wrong about the mailbox.

    The guidance is asserted as well as the name, because the name is only useful if it
    changes what the operator is told. Only an unknown outcome sends them to reconciliation,
    and only a proven non-creation invites them to draft again -- saying either of those in
    the other case is the specific mistake that creates a second draft.
    """
    provider = Outward(create=behaviour)
    client = acting(provider)
    review = (
        review_of(repository, opportunity)
        if situation == UNDECIDED
        else approve(repository, opportunity)
    )

    _, body = outward(client, review, "draft")
    assert body["outcome"] == expected
    guidance = (body.get("next") or "").casefold()
    assert ("reconcile" in guidance) is points_to_reconcile
    assert ("draft again" in guidance) is may_draft_again


def test_the_four_outcomes_are_four_distinct_names():
    """Stated once, so no surface can quietly report two of them under one name."""
    names = (outward_module.ACCEPTED, outward_module.REFUSED)
    names += (outward_module.PROVIDER_REJECTED, outward_module.UNCERTAIN)
    assert len(set(names)) == 4


# --- the property this whole PR is judged by ------------------------------------------------------


def uncertain(acting, repository, opportunity):
    """Drive the surface into an uncertain attempt and hand back the pieces to prod at it."""
    provider = Outward(create=lambda key: OSError("the connection was reset"))
    client = acting(provider)
    review = approve(repository, opportunity)
    status, body = outward(client, review, "draft")
    assert (status, body["outcome"]) == (200, "uncertain")
    assert provider.creates == 1
    assert repository.intent(review) == ("uncertain", None)
    return client, provider, review


def test_a_second_draft_after_an_uncertain_contact_creates_nothing(acting, repository, opportunity):
    """The review criterion, stated as a test.

    A draft-create request may have reached the mailbox. Asking again is the one thing that
    must not produce a second one, and it must not: `draft()` stops on the intent it finds
    before it composes anything, so this request writes nothing and contacts nothing. The
    count is what proves it -- the response alone could say the right words while a second
    draft sat in the operator's mailbox.
    """
    client, provider, review = uncertain(acting, repository, opportunity)

    status, body = outward(client, review, "draft")
    assert provider.creates == 1, "a second draft reached the provider"
    assert status == 200
    assert body["outcome"] == "uncertain"
    assert "reconcile" in body["next"].casefold()
    assert repository.intent(review) == ("uncertain", None)


def test_asking_repeatedly_never_wears_the_refusal_down(acting, repository, opportunity):
    """Stated separately, because "it refuses once" and "it always refuses" differ.

    A guard that released the intent after reporting on it, or counted attempts, would pass
    the test above and fail here. There is no number of requests that turns an unresolved
    attempt back into permission to create.
    """
    client, provider, review = uncertain(acting, repository, opportunity)

    for _ in range(5):
        status, body = outward(client, review, "draft")
        assert (status, body["outcome"]) == (200, "uncertain")
    assert provider.creates == 1
    assert repository.intent(review) == ("uncertain", None)


def test_nothing_retries_the_draft_on_its_own(acting, repository, opportunity):
    """No automatic retry: one request reached the provider exactly once, and then stopped.

    Asserted on the provider rather than on a log, because "CareerSignal never retries" is a
    claim about how many times a mailbox was contacted, not about what was written down.
    """
    _, provider, _ = uncertain(acting, repository, opportunity)
    assert (provider.creates, provider.lookups) == (1, 0)


def test_an_uncertain_attempt_locks_the_decision_rather_than_inviting_reapproval(
    acting, repository, opportunity
):
    """The other half of the safety property: the packet cannot move out from under it.

    If the decision could be changed while an attempt is unresolved, reconciliation could
    settle a provider result against material that was never what the attempt carried.
    """
    client, _, review = uncertain(acting, repository, opportunity)

    status, body = decide(
        client, review, {"approved": True, "expected": packet(repository, opportunity)}
    )
    assert status == 409
    assert body["error"] == "decision_refused"
    assert repository.intent(review) == ("uncertain", None)


# --- reconciliation -------------------------------------------------------------------------------


def test_reconciling_a_found_draft_confirms_the_attempt_and_creates_nothing(
    acting, repository, opportunity
):
    """The way out of uncertainty: read the destination, record what was already there."""
    client, provider, review = uncertain(acting, repository, opportunity)
    provider._found = "gmail-draft:found-it"

    status, body = outward(client, review, "reconcile")
    assert status == 200
    assert body["outcome"] == "accepted"
    assert body["receipt"] == "gmail-draft:found-it"
    # Read, never written: reconciliation has no authority to create a draft.
    assert provider.lookups == 1
    assert provider.creates == 1, "reconciliation created a second draft"
    assert repository.intent(review) == ("confirmed", "gmail-draft:found-it")


def test_reconciling_when_the_provider_cannot_say_leaves_the_attempt_unresolved(
    acting, repository, opportunity
):
    """Unknown is not evidence of absence.

    A lookup that finds nothing has not established that nothing exists -- it has established
    that this search did not find it. Turning that into permission to draft again is the
    single most dangerous thing this surface could do, so the intent stays exactly as it was.
    """
    client, provider, review = uncertain(acting, repository, opportunity)

    status, body = outward(client, review, "reconcile")
    assert status == 200
    assert body["outcome"] == "uncertain"
    assert "reconcile" in body["next"].casefold()
    assert provider.lookups == 1
    assert repository.intent(review) == ("uncertain", None)

    # And the attempt is still not free: drafting again after a fruitless reconciliation
    # creates nothing, exactly as before it.
    status, body = outward(client, review, "draft")
    assert (status, body["outcome"]) == (200, "uncertain")
    assert provider.creates == 1


def test_reconciling_with_an_unverifiable_identity_is_refused_and_changes_nothing(
    acting, repository, opportunity
):
    """Fail closed. A lookup made with a credential that cannot be proven would search a
    mailbox this attempt was never made against, and report its silence as evidence."""
    client, provider, review = uncertain(acting, repository, opportunity)
    provider._identity = lambda: RuntimeError("the credential could not be verified")
    provider._found = "gmail-draft:found-it"

    status, body = outward(client, review, "reconcile")
    assert status == 409
    assert body["outcome"] == "refused"
    # Never asked, because it could not be trusted to be asking the right mailbox.
    assert provider.lookups == 0
    # The existing attempt is untouched and still authoritative.
    assert repository.intent(review) == ("uncertain", None)
    assert body["state"] == "uncertain"


def test_reconciling_a_confirmed_attempt_repeats_its_receipt_without_looking(
    acting, repository, opportunity
):
    """Idempotent: a settled receipt is a fact about an attempt already made."""
    client, provider, review = uncertain(acting, repository, opportunity)
    provider._found = "gmail-draft:found-it"
    outward(client, review, "reconcile")
    assert repository.intent(review)[0] == "confirmed"

    for _ in range(3):
        status, body = outward(client, review, "reconcile")
        assert status == 200
        assert body["outcome"] == "accepted"
        assert body["receipt"] == "gmail-draft:found-it"
    # One lookup in total, from the reconciliation that actually resolved it.
    assert provider.lookups == 1
    assert provider.creates == 1


def test_drafting_a_confirmed_review_repeats_the_receipt_and_creates_nothing(
    acting, repository, opportunity
):
    """Replaying an outward request for a settled attempt returns what already exists."""
    provider = Outward()
    client = acting(provider)
    review = approve(repository, opportunity)
    _, first = outward(client, review, "draft")
    assert first["outcome"] == "accepted"

    for _ in range(3):
        status, body = outward(client, review, "draft")
        assert status == 200
        assert body["outcome"] == "accepted"
        assert body["receipt"] == first["receipt"]
    assert provider.creates == 1


# --- two operators, one review --------------------------------------------------------------------


def test_a_draft_arriving_while_another_is_in_flight_is_refused_not_duplicated(
    acting, repository, opportunity
):
    """Two tabs, one review, and the reservation is the database's to grant.

    The second request arrives while the first is still inside the provider call, so the
    first's intent is reserved and unsettled. The second is told the attempt is already under
    way rather than being given one to make, and it never reaches the provider.

    Sequenced deliberately rather than raced on timing: the interesting window is exactly the
    one where an intent is `attempting`, and a test that merely fired two requests would
    usually miss it -- the first tends to finish outright, which is the idempotent replay
    covered separately above, not this.

    It has to be settled transactionally. An in-memory guard in the handler would hold for
    one process, and this surface is threaded while the claim is a write the database
    serialises.
    """
    inside, release = threading.Event(), threading.Event()

    class Racing(Outward):
        def create(self, key, body, *, to="", subject_line=""):
            inside.set()
            assert release.wait(timeout=10), "the second request never completed"
            return super().create(key, body, to=to, subject_line=subject_line)

    provider = Racing()
    client = acting(provider)
    review = approve(repository, opportunity)

    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(lambda: outward(Client(client.surface), review, "draft"))
        assert inside.wait(timeout=10), "the first request never reached the provider"
        # The window this test is about: a claim stands, and nothing has settled it.
        assert repository.intent(review) == ("attempting", None)

        second_status, second_body = outward(Client(client.surface), review, "draft")
        release.set()
        first_status, first_body = first.result(timeout=15)

    # Exactly one draft reached the mailbox, and it is the first request's.
    assert provider.creates == 1, "both requests reached the provider"
    assert (first_status, first_body["outcome"]) == (200, "accepted")
    # The loser is told what is true, under the status a conflict with stored state gets,
    # and pointed at the one safe next move rather than invited to try again.
    assert second_status == 409
    assert second_body["outcome"] == "refused"
    assert second_body["state"] == "attempting"
    assert second_body["receipt"] is None
    assert "reconcile" in second_body["next"].casefold()
    assert "draft again" not in second_body["next"].casefold()
    # One intent, and it is the winner's.
    assert repository.intent(review) == ("confirmed", first_body["receipt"])


# --- what the outward commands refuse to be sent ---------------------------------------------------


@pytest.mark.parametrize("command", ("draft", "reconcile"))
@pytest.mark.parametrize(
    ("payload", "why"),
    (
        (b'{"approved": true}', "a decision field"),
        (b'{"expected": {}}', "an expectation"),
        (b'{"provider": "gmail"}', "a destination"),
        (b'{"actor": "someone"}', "an actor"),
        (b'{"review": "x"}', "the id it already carries in the address"),
        (b"[]", "a list rather than an object"),
        (b'"draft"', "a bare string"),
        (b"not json at all", "something that is not JSON"),
    ),
)
def test_an_outward_command_carrying_anything_is_refused(
    acting, repository, opportunity, command, payload, why
):
    """The address is the whole of the request, so a body with content is a misunderstanding.

    Refused rather than ignored, on the same ground as a rejection that carries an
    expectation: a caller that sent a field believed it was being honoured. A draft is
    exactly the request where quietly dropping one would matter -- `actor`, `provider` and
    `expected` are all things this surface decides for itself, and accepting them silently
    would read as though a page could choose them.
    """
    provider = Outward()
    client = acting(provider)
    review = approve(repository, opportunity)

    status, body = outward(client, review, command, body=payload)
    assert status == 400, why
    assert "error" in body
    assert provider.creates == 0, why
    assert repository.intent(review) is None, why


@pytest.mark.parametrize("command", ("draft", "reconcile"))
def test_an_outward_command_refuses_an_unknown_query_parameter(
    acting, repository, opportunity, command
):
    provider = Outward()
    client = acting(provider)
    review = approve(repository, opportunity)
    settings = {
        "method": "POST",
        "origin": True,
        "content_type": "application/json",
        "body": b"{}",
    }
    status, _, _ = client.send(f"/api/v1/reviews/{part(review)}/{command}?force=true", **settings)
    assert status == 400
    assert provider.creates == 0


@pytest.mark.parametrize("command", ("draft", "reconcile"))
def test_an_outward_command_needs_the_same_provenance_every_command_needs(
    acting, repository, opportunity, command
):
    """A write with no Origin has nothing to say for itself, and a token is not optional.

    Asserted per command rather than assumed from the decision route: these two reach a
    mailbox, which is the one place where inheriting a guarantee by proximity is not good
    enough.
    """
    provider = Outward()
    client = acting(provider)
    review = approve(repository, opportunity)
    address = f"/api/v1/reviews/{part(review)}/{command}"
    sent = {"method": "POST", "content_type": "application/json", "body": b"{}"}

    assert client.unread(address, origin=True, token=False, **sent) in (401, None)
    assert client.unread(address, origin=False, **sent) in (403, None)
    assert client.unread(address, origin="http://evil.example.com", **sent) in (403, None)
    assert client.unread(address, origin=True, host="evil.example.com", **sent) in (403, None)
    assert provider.creates == 0
    assert repository.intent(review) is None


def test_a_launch_without_outward_authority_refuses_and_contacts_nothing(
    addressed, repository, opportunity
):
    """The Gmail launch with no compose credential: decisions yes, drafts no.

    The address exists and answers honestly rather than 404-ing, because the operator's
    question was reasonable and the answer is about this launch. It is a local refusal in the
    exact sense the outcomes define -- certain, nothing created, the approval untouched.
    """
    client = Client(addressed)
    review = approve(repository, opportunity)
    assert client.json("/api/v1/session")[1]["outward"] is False

    for command in ("draft", "reconcile"):
        status, body = outward(client, review, command)
        assert status == 409
        assert body["outcome"] == "refused"
        assert body["state"] is None and body["receipt"] is None
        assert repository.intent(review) is None
    # The approval it could not act on is exactly as it was.
    assert repository.opportunity(opportunity)["action"]["decision"] == "approved"


def test_a_launch_with_outward_authority_says_so(acting, repository):
    assert acting(Outward()).json("/api/v1/session")[1]["outward"] is True


def test_two_drafts_that_pass_the_replay_check_together_still_make_one_draft(
    acting, repository, opportunity
):
    """The transactional half of the reservation, pinned on its own.

    The test above sequences the second request after the first has an intent, so it stops at
    the replay read and never reaches the claim. That leaves the claim itself unproven -- and
    the claim is the guard that matters when two requests genuinely overlap, because both
    then see no intent at all and neither can be stopped by reading.

    Here both are released past every local check at the same moment, so the only thing left
    between them and the mailbox is the write the database serialises. Exactly one draft is
    created. Which request wins is not asserted, because that is the database's to decide;
    that only one of them reaches the provider is the whole property.

    An in-memory mutex in the handler would pass this and fail in the way that matters, since
    the repository -- not the process -- is the source of truth. What proves the difference is
    that removing the refusal inside `claim()` makes this fail.
    """
    entered = threading.Barrier(2, timeout=15)
    answered = threading.Event()

    class Racing(Outward):
        def identity(self):
            # Past the replay read, before the claim: the window where neither request can
            # see the other except through the database.
            entered.wait()
            return super().identity()

        def create(self, key, body, *, to="", subject_line=""):
            # Whichever request won the claim holds its intent in `attempting` until the
            # other has been answered. Without this the winner finishes outright and the
            # loser reads a settled intent, which is the replay path rather than the race --
            # and a claim that had stopped refusing would go unnoticed.
            #
            # The wait is bounded well inside the client's own timeout so that a claim which
            # stopped refusing fails here as two create calls -- the thing actually being
            # measured -- rather than as a socket timing out with nothing to say.
            answered.wait(timeout=3)
            return super().create(key, body, to=to, subject_line=subject_line)

    provider = Racing()
    client = acting(provider)
    review = approve(repository, opportunity)

    def attempt():
        try:
            return outward(Client(client.surface), review, "draft")
        finally:
            answered.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(attempt) for _ in range(2)]
        results = [future.result(timeout=20) for future in futures]

    assert provider.creates == 1, "both requests reached the provider"
    # Both were answered, and neither was an error: one made the draft, the other was told
    # about the attempt that already existed.
    assert sorted(status for status, _ in results) in ([200, 200], [200, 409])
    assert all(body["outcome"] in {"accepted", "refused", "uncertain"} for _, body in results)
    # One intent, and it is the one the single provider call produced.
    state, receipt = repository.intent(review)
    assert (state, receipt) == ("confirmed", f"gmail-draft:{review}")


# --- one answer, two surfaces ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expected", "behaviour"),
    [
        ("accepted", None),
        ("provider_rejected", lambda key: ProviderRejected("declined")),
        ("uncertain", lambda key: OSError("reset")),
    ],
)
def test_the_browser_and_the_command_line_describe_the_same_attempt_identically(
    acting, repository, tmp_path, expected, behaviour
):
    """The same situation, run through both surfaces, compared field by field.

    An outward outcome is the one answer in this system where two surfaces disagreeing would
    be a safety defect rather than an inconsistency. If the browser called an unknown outcome
    a failure while the command line called it uncertain, an operator moving between them
    would be told two different things about whether a draft exists in their mailbox.

    Only `next` is allowed to differ, and only because it names where the operator is: one
    says to run a command, the other describes what to press. Everything else -- the outcome,
    the durable state, the receipt, the message -- is a fact about storage and is single
    sourced in `system.outward`.
    """

    def run(store, provider, surface):
        Intake(store, Profile(("python", "sql"))).intake_message(alert())
        target = store.opportunities()[0]["id"]
        review = approve(store, target)
        return surface(store, provider, review)

    def through_the_browser(store, provider, review):
        client = acting(provider)
        client.surface.repository = store
        client.surface.actions = OutwardActions(store, provider)
        return outward(client, review, "draft")[1]

    def through_the_command_line(store, provider, review):
        return outward_module.attempt(
            store,
            "draft",
            review,
            lambda: OutwardActions(store, provider).draft(review),
            cli.GUIDANCE,
        )

    browser = run(Repository(tmp_path / "browser"), Outward(create=behaviour), through_the_browser)
    terminal = run(
        Repository(tmp_path / "terminal"), Outward(create=behaviour), through_the_command_line
    )

    assert browser["outcome"] == terminal["outcome"] == expected
    shared = ("command", "outcome", "state", "message")
    assert {key: browser[key] for key in shared} == {key: terminal[key] for key in shared}
    # The receipt is the same fact with the review id in it, so it is compared by shape.
    assert bool(browser["receipt"]) == bool(terminal["receipt"])
    # `next` is either the same sentence on both -- most of these are facts about the record,
    # not about the surface -- or the one the guidance map supplies, where the terminal names
    # a command to type and the browser never does.
    assert "careersignal" not in (browser.get("next") or "")
    if browser.get("next") != terminal.get("next"):
        assert "careersignal" in terminal["next"]


# --- the boundary decides, not the exception class -------------------------------------------------
#
# The classifier once inferred which side of the provider-write boundary a failure fell on
# from its exception class. That cannot work: a ValueError raised inside `create()` and a
# ValueError raised by a missing approval are the same class and opposite facts. It reported
# the second answer for the first -- REFUSED, "nothing was created" -- while the row said
# `uncertain`. These pin the shape that replaced it.


@pytest.mark.parametrize(
    ("raised", "why"),
    [
        (
            lambda key: ValueError("the provider raised a ValueError"),
            "a class the old reader called refused",
        ),
        (lambda key: KeyError("missing"), "another of them"),
        (lambda key: Exception("something nobody anticipated"), "a bare Exception"),
        (lambda key: TypeError("wrong shape"), "a programming error inside the provider"),
        (lambda key: OSError("connection reset"), "the transport case that always worked"),
    ],
)
def test_any_failure_after_the_request_was_sent_reports_the_uncertainty_on_record(
    acting, repository, opportunity, raised, why
):
    """Whatever the provider raises past `create()`, the answer is the row, not the class.

    The report and the database have to agree. A response saying nothing was created, beside
    a row saying the outcome is unknown, is the one contradiction this whole vocabulary
    exists to prevent -- and it is worse than a wrong label, because "nothing was created"
    is an invitation to draft again.
    """
    provider = Outward(create=raised)
    client = acting(provider)
    review = approve(repository, opportunity)

    status, body = outward(client, review, "draft")
    assert provider.creates == 1, why
    # The record and the report say the same thing.
    assert repository.intent(review) == ("uncertain", None), why
    assert body["outcome"] == "uncertain", why
    assert body["state"] == "uncertain", why
    assert status == 200, why
    # And it points at the only safe move, never at drafting again.
    assert "reconcile" in body["next"].casefold(), why
    assert "draft again" not in body["next"].casefold(), why

    # Asking again still creates nothing, which is the property the mislabel endangered.
    outward(client, review, "draft")
    assert provider.creates == 1, why


def test_a_receipt_that_cannot_be_recorded_is_uncertain_rather_than_lost(
    acting, repository, opportunity
):
    """The draft exists and we failed to write down which one it is.

    Uncertainty about our own record rather than about the mailbox, and no safer: a second
    draft would be a real second draft. Reconciliation is still the only way out.
    """
    provider = Outward()
    client = acting(provider)
    review = approve(repository, opportunity)

    broken = OutwardActions(repository, provider)
    original = repository.finish

    def refuse_to_record(review_id, receipt):
        if receipt is not None:
            raise sqlite3.OperationalError("the database would not take the receipt")
        return original(review_id, receipt)

    client.surface.actions = broken
    broken.repository = _Recording(repository, refuse_to_record)

    status, body = outward(client, review, "draft")
    assert provider.creates == 1
    assert body["outcome"] == "uncertain"
    assert "reconcile" in body["next"].casefold()
    # It was the settling write that failed, so the reserved intent is still `attempting`
    # rather than `uncertain`. `DraftUncertain` promises an unsettled intent, not which one --
    # and this is the case that keeps the narrower promise from being made.
    state = repository.intent(review)[0]
    assert state == "attempting"
    # What makes that safe, asserted rather than assumed: both unsettled states queue as
    # reconcile, and neither lets a second artifact be created.
    assert (
        views.queue(
            repository.opportunity(opportunity), repository.opportunity(opportunity)["action"]
        )
        == "reconcile"
    )
    outward(client, review, "draft")
    assert provider.creates == 1


class _Recording:
    """The repository with one method replaced, so a persistence failure can be staged.

    Written as a wrapper rather than a monkeypatch because the surface and the service hold
    the same handle, and only the service's write is meant to fail here.
    """

    def __init__(self, repository, finish):
        self._repository, self.finish = repository, finish

    def __getattr__(self, name):
        return getattr(self._repository, name)


def test_a_reconciliation_that_fails_after_looking_leaves_the_attempt_unresolved(
    acting, repository, opportunity
):
    """A lookup that broke is not evidence the draft is absent, and must never read as one."""
    client, provider, review = uncertain(acting, repository, opportunity)

    def broken_lookup(key):
        raise ValueError("the drafts listing came back unreadable")

    provider.lookup = broken_lookup

    status, body = outward(client, review, "reconcile")
    assert body["outcome"] == "uncertain"
    assert body["state"] == "uncertain"
    assert "reconcile" in body["next"].casefold()
    # Untouched, and still not free for another draft.
    assert repository.intent(review) == ("uncertain", None)
    outward(client, review, "draft")
    assert provider.creates == 1


def test_a_refusal_before_contact_still_reports_whatever_the_record_holds(
    acting, repository, opportunity
):
    """The other direction of the same rule, so it is a rule and not a special case.

    A local refusal with an intent already standing must not report `state: None`. The
    refusal is about this invocation; the intent is a fact about an earlier one.
    """
    client, provider, review = uncertain(acting, repository, opportunity)
    provider._identity = lambda: RuntimeError("the credential could not be verified")

    status, body = outward(client, review, "reconcile")
    assert (status, body["outcome"]) == (409, "refused")
    assert body["state"] == "uncertain", "the refusal erased an intent that still exists"
    assert repository.intent(review) == ("uncertain", None)


def test_a_launch_without_authority_still_reports_an_intent_that_already_exists(
    acting, addressed, repository, opportunity
):
    """A refusal about this launch is not a claim that nothing is out there.

    An uncertain attempt made from the command line, or from an earlier launch that held a
    credential, survives a restart without one. Reporting `state: None` over it would tell
    the operator there is nothing to reconcile, which is the same collapse as mislabelling
    the outcome -- arrived at from the other direction.
    """
    # An uncertain attempt, made while the surface could reach a provider.
    client, provider, review = uncertain(acting, repository, opportunity)

    # The same database, served by a launch with no outward authority at all.
    without = Client(addressed)
    assert without.json("/api/v1/session")[1]["outward"] is False

    status, body = outward(without, review, "reconcile")
    assert (status, body["outcome"]) == (409, "refused")
    assert body["state"] == "uncertain", "the refusal erased an attempt that still exists"
    # Untouched, uncontacted, and still not free for another draft.
    assert repository.intent(review) == ("uncertain", None)
    assert provider.creates == 1


def test_a_refusal_raised_before_contact_reports_the_intent_it_did_not_touch(
    acting, repository, opportunity
):
    """A pre-contact `ValueError` with an attempt already standing.

    Reconciling through a provider declared for another mailbox is refused before anything is
    searched -- the right answer, and one the classifier once reported as `state: None`. The
    refusal is about this request; the unresolved attempt is a fact about an earlier one, and
    dropping it would say there is nothing to reconcile.
    """
    client, provider, review = uncertain(acting, repository, opportunity)

    class Elsewhere(Outward):
        provider, namespace = "gmail", "gmail:somebody.else@example.com"

    stranger = Elsewhere()
    client.surface.actions = OutwardActions(repository, stranger)

    status, body = outward(client, review, "reconcile")
    assert (status, body["outcome"]) == (409, "refused")
    assert body["state"] == "uncertain", "a pre-contact refusal erased the standing attempt"
    # Never searched, because it could not be trusted to be searching the right mailbox.
    assert stranger.lookups == 0
    assert repository.intent(review) == ("uncertain", None)


def test_a_fault_over_an_unsettled_attempt_reports_it_rather_than_escaping(
    acting, repository, opportunity
):
    """The safe direction when something breaks that this vocabulary has no name for.

    A fault is not a state, and with nothing reserved it is still raised as the fault it is.
    But an unsettled intent outranks that: something may exist in the mailbox, and letting the
    exception escape would hand the operator a traceback where the record has an answer.
    """
    client, provider, review = uncertain(acting, repository, opportunity)

    def broken(review_id):
        raise sqlite3.OperationalError("the intent row could not be read")

    client.surface.actions = OutwardActions(_Recording(repository, repository.finish), provider)
    client.surface.actions.repository.intent_identity = broken

    status, body = outward(client, review, "reconcile")
    assert status == 200
    assert body["outcome"] == "uncertain"
    assert body["state"] == "uncertain"
    assert "reconcile" in body["next"].casefold()
    assert provider.lookups == 0
    assert repository.intent(review) == ("uncertain", None)
