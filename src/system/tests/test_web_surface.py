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

import pytest

from communications.gmail import TOKEN_VARIABLE
from communications.gmail_draft import COMPOSE_TOKEN_VARIABLE
from communications.message import Message
from data import store
from data.repository import Repository
from recruiting.models import Profile
from system import cli
from system.web import server as web
from system.workflow import Intake

JOB_TEXT = (
    "Title: IAM Architect\r\n"
    "Company: Example Corp\r\n"
    "Location: remote\r\n"
    "Skills: Python, SQL\r\n"
    "URL: https://jobs.example.com/roles/1\r\n"
)
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "FROBNICATE")


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

    def send(self, path, *, method="GET", token=True, host=None, headers=(), body=None):
        connection = http.client.HTTPConnection(web.LOOPBACK, self.surface.server_port, timeout=10)
        try:
            connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            connection.putheader("Host", self.surface.authority if host is None else host)
            if token:
                connection.putheader(
                    web.TOKEN_HEADER,
                    self.surface.token if token is True else token,
                )
            for name, value in headers:
                connection.putheader(name, value)
            if body is not None:
                connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body)
            response = connection.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            connection.close()

    def json(self, path, **kwargs):
        status, payload, headers = self.send(path, **kwargs)
        return status, json.loads(payload)


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
        "/bootstrap.js",
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
    assert b'<script src="bootstrap.js"' in payload
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


@pytest.mark.parametrize("method", WRITE_METHODS)
def test_every_method_but_get_and_head_is_refused_without_reaching_a_route(
    client, repository, method
):
    """Including verbs this server has never heard of: refusal is the default."""
    before = state(repository)
    status, payload, headers = client.send(method=method, path="/api/v1/opportunities", body=b"{}")
    assert status == 405
    assert headers["Allow"] == "GET, HEAD"
    assert json.loads(payload) == {"error": "This surface is read-only"}
    assert state(repository) == before


@pytest.mark.parametrize("method", WRITE_METHODS)
def test_a_write_verb_is_refused_even_with_a_valid_token(client, surface, method):
    """Authority is not the question. There is no write for a valid token to reach."""
    assert client.send(method=method, path="/api/v1/opportunities")[0] == 405


def test_a_write_verb_is_refused_before_the_token_is_even_considered(client):
    """Earliest possible refusal: a cross-origin POST never gets as far as 401 or 403."""
    status, _, headers = client.send(
        "/api/v1/session", method="POST", token=False, host="evil.example.com"
    )
    assert status == 405 and headers["Allow"] == "GET, HEAD"


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
    assert set(surface.assets) == {"index.html", "bootstrap.js", "careersignal.css"}
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
# Names that write, decide, contact a provider, or carry a credential. None may appear
# anywhere in this package, as a call, an attribute or an import.
FORBIDDEN_NAMES = frozenset(
    {
        "Intake",
        "OutwardActions",
        "GmailDrafts",
        "GmailReader",
        "GmailCredentials",
        "GmailComposeCredentials",
        "ControlledDrafts",
        "decide",
        "record_status",
        "claim",
        "finish",
        "refuse",
        "reject",
        "ingest",
        "intake",
        "intake_message",
        "draft",
        "reconcile",
        "migrate",
        "execute",
        "executemany",
        "commit",
        "transaction",
        "connection",
    }
)


def test_the_package_reaches_no_write_and_no_second_business_layer():
    """Read mechanically, because a passing response body cannot show this.

    A route that started deciding something of its own would answer 200 exactly as before.
    What says otherwise is the source: this package may name the repository's read
    projections and nothing else, and may not name a write, a provider or a credential at
    all -- so a future edit that reaches one fails here rather than at review.
    """
    for name, tree in package_modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                assert node.attr not in FORBIDDEN_NAMES, f"{name}: .{node.attr}"
                if isinstance(node.value, ast.Name) and node.value.id == "repository":
                    assert node.attr in PERMITTED_READS, f"{name}: repository.{node.attr}"
            if isinstance(node, ast.Name):
                assert node.id not in FORBIDDEN_NAMES, f"{name}: {node.id}"


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
    """Eight routes, all read. A ninth has to be written where the eight are."""
    tree = dict(package_modules())["server.py"]
    projection = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_projection"
    )
    matches = [node for node in ast.walk(projection) if isinstance(node, ast.Match)]
    assert len(matches) == 1
    assert len(matches[0].cases) == 8


# --- the command ----------------------------------------------------------------------------------


def invoke(monkeypatch, *arguments):
    """Run `careersignal ...` with the surface itself replaced by a recorder.

    The command's job is to hand the right database and port to the surface and then block;
    what the surface does with them is the rest of this file.
    """
    served = []
    monkeypatch.setattr(cli, "serve", lambda repository, *, port: served.append((repository, port)))
    monkeypatch.setattr("sys.argv", ["careersignal", *arguments])
    cli.main()
    return served


def test_the_command_serves_the_named_database_on_the_named_port(monkeypatch, capsys, tmp_path):
    served = invoke(monkeypatch, "serve", "--db", str(tmp_path / "db"), "--port", "9999")
    assert len(served) == 1
    repository, port = served[0]
    assert (repository.path, port) == (store.database_path(tmp_path / "db"), 9999)
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
