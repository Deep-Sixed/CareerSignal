"""What the shipped frontend is, read from the files themselves.

These are structural and security tests, not browser-engine tests. Nothing here executes
JavaScript or lays anything out; adding a runtime that could would mean adding Node to
twelve CI cells to prove properties of a second implementation, which is the outcome this
whole change exists to avoid. Visual acceptance is done by eye against the frozen renders.

What these can prove is exactly what matters most about a page that holds a launch
credential: that it reaches no write, names no route the server does not have, sends the
token, can never turn stored text into markup, loads nothing from the network, and keeps
no second copy of a rule that Python owns.
"""

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from recruiting.status import STATUSES
from system import views

STATIC = Path(__file__).resolve().parents[1] / "web" / "static"
SCRIPTS = sorted(STATIC.glob("*.js"))
# Every route the server actually has, as the first segment under /api/v1.
ROUTES = ("session", "opportunities", "communications", "reviews", "timeline")
# Sinks that parse a string as markup or as code. None may appear anywhere.
SINKS = (
    "innerHTML",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "eval(",
    "new Function",
    "setHTML",
    "srcdoc",
    "javascript:",
)
WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
COMMENT = re.compile(r"/\*.*?\*/|(?<![:\"'])//[^\n]*", re.S)


def source(path) -> str:
    return path.read_text(encoding="utf-8")


def code(path) -> str:
    """The file with its comments removed, so prose about a rule is not read as the rule."""
    return COMMENT.sub(" ", source(path))


@pytest.fixture(scope="module")
def page():
    return source(STATIC / "index.html")


def test_the_frontend_is_the_files_this_test_thinks_it_is():
    """A guard on the guard: a renamed or added script must not slip past these checks."""
    assert {path.name for path in SCRIPTS} == {"app.js", "api.js", "dom.js", "screens.js"}
    assert {path.name for path in STATIC.iterdir()} == {
        "index.html",
        "careersignal.css",
        "icon.svg",
        "app.js",
        "api.js",
        "dom.js",
        "screens.js",
    }


# --- nothing stored can become markup --------------------------------------------------------


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda path: path.name)
def test_no_script_can_turn_a_string_into_markup_or_code(path):
    """The single most important property of this origin.

    A recruiter writes the subject line, the job title, the excerpt and the receipt. This
    page holds the launch credential. Values reach the DOM as `textContent` on nodes built
    by `document.createElement`, so there is no parser for a payload to reach -- which is a
    stronger claim than escaping, because escaping can be forgotten at one call site.
    """
    body = code(path)
    for sink in SINKS:
        assert sink not in body, f"{path.name} reaches {sink}"
    assert ".textContent" in source(STATIC / "dom.js")


def test_text_assignment_happens_in_one_place():
    """One file to audit, not four.

    Every value reaches the DOM through `el()` or `text()`, both of which live in dom.js
    and both of which run it through `visible()` first. A screen that assigned textContent
    directly would be safe today and one careless edit from not being, so the rule is that
    no screen assigns it at all.
    """
    for script in SCRIPTS:
        written = code(script).count("textContent")
        assert written == (2 if script.name == "dom.js" else 0), script.name


def test_the_display_of_non_printing_bytes_escapes_and_never_deletes():
    """The frozen design's rule: control characters are shown, never removed.

    A title carrying an escape sequence is evidence. Dropping it would hide from the
    operator that the stored record contains it, and stripping at the edge would make the
    surface disagree with what the database holds.
    """
    body = code(STATIC / "dom.js")
    assert "visible" in body and "charCodeAt" in body
    assert 'replace(NONPRINTING, "")' not in body
    assert "\\\\x" in body, "the replacement no longer writes an escape"


def test_an_href_is_the_one_value_checked_rather_than_only_written():
    """textContent cannot protect an attribute, so the one attribute taken from stored
    text is restricted to http and https -- a stored `javascript:` URL renders as text."""
    body = code(STATIC / "dom.js")
    assert "https?" in body and "anchor.href" in body


# --- no write authority ------------------------------------------------------------------------


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda path: path.name)
def test_no_script_can_make_a_request_that_is_not_a_get(path):
    body = code(path)
    for method in WRITE_METHODS:
        assert f'"{method}"' not in body and f"'{method}'" not in body, path.name
    assert "method:" not in body.replace(" ", ""), path.name
    assert "XMLHttpRequest" not in body and "sendBeacon" not in body
    assert "<form" not in body and "requestSubmit" not in body


def test_every_request_goes_through_the_one_module_that_holds_the_credential():
    """`fetch` appears once, in api.js, inside a helper with no method parameter."""
    for path in SCRIPTS:
        found = code(path).count("fetch(")
        assert found == (1 if path.name == "api.js" else 0), path.name
    body = code(STATIC / "api.js")
    assert "X-CareerSignal-Token" in body
    assert 'credentials: "omit"' in body


def test_every_path_the_frontend_asks_for_is_a_route_the_server_has():
    """A typo would be a 404 at runtime; here it is a failing test with the name in it."""
    whole = code(STATIC / "api.js")
    assert '"/api/v1"' in whole, "the root the server serves under is not declared here"
    # The root is declared once and is not itself a route, so it is removed before the
    # path literals are read.
    body = re.sub(r'const ROOT = "[^"]*";', " ", whole)
    # Each request path is written whole, so the first segment of every path literal is a
    # route name rather than a fragment spliced on somewhere else.
    asked = set(re.findall(r"[\"`]/([a-z_]+)", body))
    assert asked, "no request paths were found, so this guard is checking nothing"
    assert asked == set(ROUTES), asked ^ set(ROUTES)


def test_the_credential_leaves_the_address_bar_before_anything_else_runs():
    body = code(STATIC / "api.js")
    assert "sessionStorage" in body and "localStorage" not in body
    assert "replaceState" in body


# --- nothing is loaded from the network ---------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [*SCRIPTS, STATIC / "index.html", STATIC / "careersignal.css", STATIC / "icon.svg"],
    ids=lambda path: path.name,
)
def test_no_asset_names_an_external_resource(path):
    """Zero frontend dependencies, enforced rather than intended.

    No CDN, no font service, no analytics: the surface has to work on a machine with no
    network at all, and a page that fetches from elsewhere would leak that this operator is
    job-hunting to whoever serves it.
    """
    # The SVG namespace is an identifier, not an address: nothing fetches it.
    body = code(path).replace('xmlns="http://www.w3.org/2000/svg"', " ")
    for absolute in ('"http://', "'http://", '"https://', "'https://", '"//', 'src="http'):
        assert absolute not in body, f"{path.name} names {absolute}"
    assert "@import" not in body and "url(" not in body


def test_the_page_carries_no_inline_script_or_style():
    """The merged policy has no 'unsafe-inline' and no 'unsafe-eval'; this keeps it viable."""
    body = source(STATIC / "index.html")
    assert not re.search(r"<script(?![^>]*\ssrc=)", body), "an inline script block"
    assert not re.search(r"\son[a-z]+=", body), "an inline event handler attribute"
    assert " style=" not in body
    for script in SCRIPTS:
        assert 'setAttribute("style"' not in code(script) and "cssText" not in code(script)


# --- the shell -------------------------------------------------------------------------------


class Structure(HTMLParser):
    """Enough of the document to assert its landmarks, ids and labels exist."""

    def __init__(self):
        super().__init__()
        self.tags, self.ids, self.attributes = [], set(), []

    def handle_starttag(self, tag, attrs):
        found = dict(attrs)
        self.tags.append(tag)
        self.attributes.append((tag, found))
        if "id" in found:
            self.ids.add(found["id"])


@pytest.fixture(scope="module")
def structure(page):
    parser = Structure()
    parser.feed(page)
    return parser


def test_the_document_declares_what_a_browser_needs(structure, page):
    assert page.lstrip().lower().startswith("<!doctype html>")
    attributes = dict(structure.attributes)
    assert attributes["html"].get("lang") == "en"
    assert any(found.get("charset") == "utf-8" for _, found in structure.attributes)
    assert any(found.get("name") == "viewport" for _, found in structure.attributes)
    assert "<title>CareerSignal</title>" in page


def test_the_three_panes_and_their_landmarks_exist(structure):
    """Navigation, list and detail: the frozen shell, as landmarks a screen reader can find."""
    assert {"nav", "main", "aside"} <= set(structure.tags)
    labelled = [
        found
        for tag, found in structure.attributes
        if tag in ("nav", "main", "aside")
        and (found.get("aria-label") or found.get("aria-labelledby"))
    ]
    assert len(labelled) >= 3, "a landmark has no accessible name"


def test_every_container_the_application_fills_is_present(structure):
    """The shell ships empty; these are the ids the screens write into."""
    assert {
        "nav",
        "session-facts",
        "list-title",
        "list-subtitle",
        "list-controls",
        "list-body",
        "detail",
    } <= structure.ids


def test_the_page_loads_only_its_own_packaged_assets(structure):
    referenced = {
        found.get("src") or found.get("href")
        for tag, found in structure.attributes
        if tag in ("script", "link")
    }
    packaged = {path.name for path in STATIC.iterdir()}
    assert referenced <= packaged, referenced - packaged
    assert "app.js" in referenced and "careersignal.css" in referenced


def test_the_five_frozen_screens_are_the_screens_that_exist():
    """Five, and no Contacts or Tasks: those are future concepts, not durable models."""
    body = code(STATIC / "screens.js")
    declared = re.search(r"export const SCREENS = \{(.*?)\n\};", body, re.S)
    assert declared, "the screen table moved and this guard stopped guarding anything"
    names = re.findall(r"^\s{2}(\w+):", declared.group(1), re.M)
    assert names == ["dashboard", "inbox", "opportunities", "approvals", "activity"]
    assert "contacts" not in body.lower() and "tasks" not in body.lower()


# --- the count, the destination and the rows agree ------------------------------------------------


def test_the_approvals_badge_is_the_approvals_screens_own_filter():
    """One definition, or the badge promises rows the screen will not show.

    The Approvals screen lists the five queues where approval state is worth reading;
    `rejected` is deliberately not one of them. A nav badge counting every queue but
    `none` would say "Approvals 1" and then show "Nothing matches." -- a count the operator
    cannot reach is a worse answer than no count, so both sides call the same predicate.
    """
    screens, app = code(STATIC / "screens.js"), code(STATIC / "app.js")
    assert screens.count("const APPROVAL_QUEUES") == 1, "the grouping is declared twice"
    assert "export function inApprovals" in screens
    assert "inApprovals" in app, "app.js counts with a filter of its own"
    assert 'queue !== "none"' not in app, "the badge uses a grouping the screen does not"
    # The screen itself filters through the same predicate rather than re-listing.
    assert "filter(inApprovals)" in screens


def test_a_queue_card_leads_only_where_its_rows_are_listed():
    """A card for a queue Approvals does not show must not route there.

    The Dashboard's rejected card would otherwise open a screen the rejected row is absent
    from: the operator presses a count and arrives at nothing.
    """
    screens = code(STATIC / "screens.js")
    assert "APPROVAL_QUEUES.includes(queue)" in screens, "every card routes the same way"
    assert "showApprovals()" in screens
    # The destination takes no queue argument, so it cannot pretend to filter by one.
    assert "showQueue(" not in screens and "showQueue(" not in code(STATIC / "app.js")


FORBIDDEN_CLAIMS = (
    # `stale` does not say which fact moved: a later message can break the binding, and so
    # can a status event.
    "moved the recipient",
    "later message moved",
    # `reconcile()` calls finish(), which updates the intent row and appends an audit
    # event. It creates no second draft; it does not write nothing.
    "writes nothing new",
    "writes nothing",
)


@pytest.mark.parametrize("claim", FORBIDDEN_CLAIMS)
def test_the_attention_copy_claims_no_cause_the_queue_does_not_guarantee(claim):
    """Display copy may name implications the server guarantees, and no others.

    The queue is the only fact behind these panels. Wording that explains it by naming a
    cause is a browser-side inference, and an operator reading a confident sentence has no
    way to tell it was guessed.
    """
    body = code(STATIC / "screens.js")
    assert claim not in body, f"the attention copy asserts: {claim}"


def test_the_attention_copy_points_at_the_evidence_instead_of_guessing():
    body = code(STATIC / "screens.js")
    assert "does not create another draft" in body
    assert "compare the approved and" in body, "stale no longer sends the operator to the digests"


# --- one implementation of what the state means -------------------------------------------------


DECLARATIONS = ("QUEUE_LABELS", "QUEUE_HINTS", "ATTENTION", "APPROVAL_QUEUES")


def without_declarations(body) -> str:
    """The screen code with its display tables removed."""
    for name in DECLARATIONS:
        # Both shapes: a table spanning lines, and a one-line array.
        body = re.sub(rf"const {name} = [\[{{].*?[\]}}];", " ", body, flags=re.S)
    return body


def test_the_browser_keeps_no_second_copy_of_a_rule():
    """The queue names may key display copy. They may not appear in logic.

    `views.queue()` decides which queue an opportunity is in, and its precedence is the
    rule -- mutation-tested in Python. If a queue name shows up outside the flat display
    tables, something in the browser has started deciding, and a list and a detail pane can
    begin to disagree about the same opportunity.
    """
    body = code(STATIC / "screens.js")
    stripped = without_declarations(body)
    for name in views.QUEUES:
        if name == "none":
            # The one comparison the browser is allowed: whether there is a chip to draw.
            continue
        assert f'"{name}"' not in stripped, f"{name} is used outside the display tables"
    for name in DECLARATIONS:
        assert f"const {name}" in body, f"{name} was renamed; this guard no longer strips it"


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda path: path.name)
def test_no_script_copies_a_vocabulary_python_owns(path):
    """Read with the display tables stripped out.

    "rejected" is both a status and a queue, so a table that gives the queue a label would
    trip a naive scan. What matters is that no vocabulary reaches the logic, and the logic
    is what is left once the tables are removed.
    """
    body = without_declarations(code(path))
    for status in STATUSES:
        assert f'"{status}"' not in body, f"{path.name} names the status {status}"
    for wording in views.DRAFTS.values():
        # The sentences, which are what `attempt()` composes. A single word like "created"
        # is not wording anybody could copy, and collides with ordinary field names.
        if " " in wording:
            assert wording not in body, f"{path.name} copies draft-state wording"
    # Property reads, not the words: the pane legitimately says "what an approval binds",
    # and prose about a rule is not the rule.
    for absent in ("TERMINAL", "advances &&", "coverage >=", ".binds", "action.decision"):
        assert absent not in body, f"{path.name} reads {absent}"


@pytest.mark.parametrize("path", sorted(STATIC.iterdir()), ids=lambda path: path.name)
def test_no_asset_ships_a_raw_control_byte(path):
    """Source files are text.

    An escape sequence written as a literal byte instead of as its escape reads as a
    perfectly ordinary file in an editor and turns the asset into something `grep` calls
    binary and a tree audit could refuse. It is also the one place a stray byte would be
    invisible in review.
    """
    raw = path.read_bytes()
    stray = {byte for byte in raw if byte < 9 or 13 < byte < 32 or byte == 127}
    assert not stray, f"{path.name} carries {[hex(byte) for byte in sorted(stray)]}"
    assert raw.decode("utf-8")


def test_the_frontend_asks_for_the_rendered_strings_rather_than_the_parts():
    """Presentation is requested explicitly, and read as a block."""
    assert "presentation=true" in code(STATIC / "api.js")
    body = code(STATIC / "screens.js")
    for supplied in (
        "presentation.coverage",
        "presentation.queue",
        "presentation.approval",
        "presentation.attempt",
    ):
        assert supplied in body, supplied
