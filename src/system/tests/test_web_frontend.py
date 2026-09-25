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

from communications.gmail import TOKEN_VARIABLE
from communications.gmail_draft import COMPOSE_TOKEN_VARIABLE
from recruiting.status import STATUSES
from system import views

STATIC = Path(__file__).resolve().parents[1] / "web" / "static"
SCRIPTS = sorted(STATIC.glob("*.js"))
# Every route the server actually has, as the first segment under /api/v1.
ROUTES = (
    "session",
    "opportunities",
    "communications",
    "reviews",
    "timeline",
    "statuses",
    "intake",
)
# Sinks that parse a string as markup or as code. None may appear anywhere.
SINKS = (
    "innerHTML",
    "srcdoc",
    "outerHTML",
    "insertAdjacentHTML",
    "document.write",
    "eval(",
    "new Function",
    "setHTML",
    "javascript:",
)
# The one verb the frontend may send, and every verb it may not.
COMMAND_METHOD = "POST"
FORBIDDEN_METHODS = ("PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")
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
def test_no_script_names_a_verb_this_surface_does_not_answer(path):
    """One command exists; nothing else may even be spelled.

    A frontend that never writes the word cannot send the request, so this is checked as a
    property of the source rather than of what happened to be exercised at runtime.
    """
    body = code(path)
    for method in FORBIDDEN_METHODS:
        assert f'"{method}"' not in body and f"'{method}'" not in body, path.name
    assert "XMLHttpRequest" not in body and "sendBeacon" not in body
    assert "<form" not in body and "requestSubmit" not in body


def test_exactly_one_command_is_sent_and_only_from_the_module_that_holds_the_credential():
    """The write surface of this whole application, as a count.

    `POST` is written once, in one file, in a helper with no method parameter -- so there
    is nothing a screen could hand a different verb to. A second command means a second
    function here, which means this number changes and a reader is made to notice.

    PR 9 added a command whose body is a message rather than fields, and it did not move this
    number: the media type and the body vary, the verb does not, and the JSON helper now
    delegates to the same one. A media type is not an authority; a method is.
    """
    for path in SCRIPTS:
        body = code(path)
        sent = body.count(f'"{COMMAND_METHOD}"') + body.count(f"'{COMMAND_METHOD}'")
        assert sent == (1 if path.name == "api.js" else 0), path.name
    body = code(STATIC / "api.js")
    assert body.count("method:") == 1, "a method is chosen outside the shared command helper"
    assert "X-CareerSignal-Token" in body
    assert 'credentials: "omit"' in body


def test_the_command_is_addressed_to_the_one_route_the_server_answers():
    body = code(STATIC / "api.js")
    assert "/status`" in body, "the command no longer names the status address"
    assert "recordStatus" in body
    # Sent as JSON, because the server refuses anything else and a silently dropped header
    # would turn every write into a 400 the operator could not explain.
    assert "Content-Type" in body and "application/json" in body


def test_the_command_carries_the_event_the_operator_was_shown():
    """A blind append is the race `expected_event_id` exists to close.

    The value has to come from the record on screen. A literal, a cached number, or the
    newest event re-read at submit time would each turn a compare-and-append back into an
    ordinary append that happens to carry a number.
    """
    body = code(STATIC / "screens.js")
    assert "expected_event_id: record.status_event" in body, (
        "the command no longer binds to the event in the detail the operator is reading"
    )


def region(body, opening, closing):
    """The text between a declaration and the line that closes it.

    Slicing to the end of the file would let a later function satisfy an assertion about
    this one -- which is exactly how the first version of the test below passed against a
    `recordStatus` that had stopped re-reading.
    """
    start = body.index(opening)
    end = body.index(closing, start)
    return body[start:end]


def test_the_browser_re_reads_after_a_command_rather_than_patching_what_it_has():
    """What the row, the queue and the counts become is the engine's answer.

    A local patch would be the browser forming a second opinion about state it had just
    asked the server to change -- the same duplication the presentation seam exists to
    prevent, arriving through the back door.
    """
    body = code(STATIC / "app.js")
    command = region(body, "recordStatus(payload)", "\n  },")
    assert "await reload()" in command, "a command no longer re-reads"
    assert "await opportunityDetail(" in command, "the detail pane is not rebuilt from the server"
    # One assembler for the pane, so the view after a command is built exactly as the view
    # after a click is.
    assert body.count("async function opportunityDetail(") == 1


def test_a_refused_command_is_shown_and_never_retried():
    body = code(STATIC / "app.js")
    command = region(body, "recordStatus(payload)", "\n  },")
    assert "refused(answer)" in command, "a refusal is not surfaced to the operator"
    assert "answer.ok" in command
    described = region(body, "function refused(", "\n}")
    assert "409" in described and "observed_event_id" in described, (
        "the refusal no longer says what the server found"
    )
    # Nothing re-sends. A retry against the newer event would be this surface deciding on
    # the operator's behalf, which is exactly what the compare-and-append refuses to do.
    assert "recordStatus(" not in command[command.index("await api.recordStatus") + 30 :]


def test_every_read_goes_through_the_same_module():
    """`fetch` appears twice in api.js -- one read helper, one command helper -- and nowhere
    else. Every command shares the second, so this count does not move when one is added."""
    for path in SCRIPTS:
        found = code(path).count("fetch(")
        assert found == (2 if path.name == "api.js" else 0), path.name


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


# OUTWARD_CONTROLS joined these in PR 8, and is the same shape as DECISION_CONTROLS beside it:
# a flat table keyed by a queue name the server sent, with no precedence, no fallthrough and no
# arithmetic. A queue absent from it offers no outward control, so the default when the engine
# grows a state this file has not been taught is to offer nothing.
DECLARATIONS = (
    "QUEUE_LABELS",
    "QUEUE_HINTS",
    "ATTENTION",
    "APPROVAL_QUEUES",
    "DECISION_CONTROLS",
    "OUTWARD_CONTROLS",
)


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


# --- the expectation is the one that was rendered -----------------------------------------------


def test_the_expectation_sent_is_the_one_the_packet_rendered():
    """The correction the PR contract turns on, asserted structurally.

    Rendering the packet from one read and fetching its expectation from another would
    reopen the exact window `decide(expected=...)` closes: the four facts posted back would
    describe a moment the operator was never shown. So the control hands over
    `bound.expected` -- a property of the same object whose recipient, subject and wording
    are rendered beside it -- and there is no second read anywhere in that path.
    """
    screens = code(STATIC / "screens.js")
    decision = region(screens, "function decisionAction(", "\nfunction statusAction(")
    # The one thing handed to the action, read off the packet being displayed.
    assert "actions.decide(bound.review, true, bound.expected)" in decision
    assert "bound.expected" in decision
    # The same object the visible fields come from, so the two cannot describe two moments.
    assert "const bound = record.bound;" in decision
    for elsewhere in ("api.", "fetch(", "authorization", "await "):
        assert elsewhere not in decision, f"the decision control reaches {elsewhere}"


def test_the_decision_sends_the_expectation_unchanged_and_never_rebuilds_one():
    """No field of the expectation is named in the browser's write path.

    A path that assembled `{content_digest: ..., ...}` could assemble it from anything --
    including a fresher read -- and the server would accept it, because a well-formed
    expectation is exactly what it is waiting for. The only safe shape is to pass through
    the object that was rendered, so naming a field at all is the thing to forbid.
    """
    app = code(STATIC / "app.js")
    decide = region(app, "  decide(review, approved, expected) {", "\n  openCommunication(")
    assert "api.decide(review, approved ? { approved, expected } : { approved })" in decide
    for field in ("content_digest", "draft_digest", "addressing_digest", "status_event_id"):
        assert field not in app, f"app.js names {field}; an expectation is being assembled"
    for field in ("content_digest", "draft_digest", "addressing_digest", "status_event_id"):
        assert field not in code(STATIC / "api.js"), f"api.js names {field}"


def test_the_browser_re_reads_after_a_decision_and_never_resubmits_it():
    """Success and refusal alike, and the refusal is never retried against the newer packet.

    Retrying would approve a packet nobody read, which is precisely what the expectation
    exists to prevent -- the browser would be laundering a stale decision into a fresh one.
    """
    app = code(STATIC / "app.js")
    decide = region(app, "  decide(review, approved, expected) {", "\n  openCommunication(")
    assert "await reload();" in decide
    assert "await opportunityDetail(id" in decide
    # One send, so no branch resubmits.
    assert decide.count("api.decide(") == 1
    # Nothing local is patched into the state the engine owns.
    for patched in ("state.opportunities[", "row.presentation", ".queue =", ".decision ="):
        assert patched not in decide, f"the decision patches {patched} locally"


def test_the_decision_controls_offer_nothing_the_queue_does_not_name():
    """Which controls exist is looked up, not decided.

    `views.queue()` owns that precedence and is mutation-tested in Python. A browser that
    worked out for itself whether an approval still binds would be a second implementation
    of the rule, and the list and the detail pane would drift apart on the first change.
    """
    screens = code(STATIC / "screens.js")
    decision = region(screens, "function decisionAction(", "\nfunction statusAction(")
    assert "DECISION_CONTROLS[record.presentation.queue]" in decision
    for derived in (".binds", "action.decision", "advances", "TERMINAL", "attempted"):
        assert derived not in decision, f"the decision control derives from {derived}"
    # A queue the table does not name offers nothing, rather than falling through to a default.
    assert "if (!bound || !target || !offered)" in decision


# --- the outward commands -------------------------------------------------------------------


def test_the_two_outward_commands_are_addressed_to_the_routes_the_server_answers():
    """Two more functions in the file that holds the credential, not a helper taking a name.

    `test_exactly_one_command_is_sent_...` above still holds: all four commands share one
    `send`, so `POST` is written once and there is nothing a screen could hand a verb to.
    What grows is the number of named functions, which is the shape that makes a reader
    notice a command being added.
    """
    body = code(STATIC / "api.js")
    assert "/draft`" in body and "/reconcile`" in body
    assert "draft:" in body and "reconcile:" in body
    # Each sends an empty object. A payload with fields would be refused by the server, and
    # assembling one here would suggest the browser gets to say something about the draft.
    assert body.count("credential, {})") == 2


def test_the_outward_controls_are_keyed_by_the_queue_the_server_decided():
    """The same shape as the decision controls: a flat table, no precedence, no arithmetic."""
    body = code(STATIC / "screens.js")
    table = region(body, "const OUTWARD_CONTROLS = {", "};")
    assert "draft:" in table and "reconcile:" in table
    # A confirmed draft offers neither command; drafting again would return the same receipt
    # and offering a button for it would suggest there is a second draft to be made.
    assert "created:" not in table


def test_the_browser_never_offers_a_second_draft_after_an_unknown_outcome():
    """The rule this whole surface is built around, as a property of the shipped file.

    After an uncertain attempt the only outward control offered is Reconcile. There is no
    Draft again, no retry, and no timer: a second draft is not something the operator may ask
    for until reconciliation has established what happened.
    """
    body = code(STATIC / "screens.js")
    table = region(body, "const OUTWARD_CONTROLS = {", "};")
    reconcile = region(table, "reconcile:", "},")
    assert "Create draft" not in reconcile
    assert "never creates a draft" in reconcile
    for absent in ("retry", "again", "setTimeout", "setInterval"):
        assert absent not in region(body, "function outwardAction(", "\n}"), absent


def test_nothing_in_the_shell_retries_an_outward_command():
    """A repeat has to be the operator asking again, deliberately."""
    body = code(STATIC / "app.js")
    outward = region(body, "  outward(review, command) {", "\n  },")
    for absent in ("setTimeout", "setInterval", "while", "for (", "catch"):
        assert absent not in outward, f"the outward action contains {absent}"
    # Send, then re-read. Nothing patches the row from the answer.
    assert "await reload()" in outward


def test_the_attempt_report_repeats_the_servers_words_rather_than_judging_them():
    """Four outcomes, four different facts about a mailbox. None of them is reworded here.

    A browser that paraphrased a proven rejection as an unknown outcome -- or the reverse --
    would be deciding what might exist in the operator's mailbox. The name, the description
    and the instruction all arrive already written by the same code the CLI reports from.
    """
    body = code(STATIC / "screens.js")
    report = region(body, "function attemptReport(", "\n}")
    for field in ("attempt.outcome", "attempt.message", "attempt.next", "attempt.receipt"):
        assert field in report, field
    # No vocabulary of its own: no outcome name is written into the branch logic.
    for outcome in ("accepted", "refused", "provider_rejected", "uncertain"):
        assert f'"{outcome}"' not in report, f"{outcome} is decided in the browser"


def test_a_launch_without_outward_authority_says_so_instead_of_offering_a_button():
    """A control that could only ever refuse teaches the operator to ignore refusals."""
    body = code(STATIC / "screens.js")
    action = region(body, "function outwardAction(", "\n}")
    assert "extra.outward" in action
    assert "records decisions only" in action


# --- taking material in --------------------------------------------------------------------


def test_the_intake_commands_are_addressed_to_the_routes_the_server_answers():
    """Two more named functions in the file that holds the credential.

    `test_exactly_one_command_is_sent_...` above still holds: all six commands share one
    `deliver`, so the verb is written once and there is nothing a screen could hand a
    different one to. What grows is the number of named functions, which is the shape that
    makes a reader notice a command being added.
    """
    body = code(STATIC / "api.js")
    assert "/intake/eml`" in body and "/intake/gmail`" in body
    assert "intakeSources:" in body and "ingestEml:" in body and "ingestGmail:" in body
    # The message is sent as a message. Nothing wraps it to fit the JSON helper.
    assert "message/rfc822" in body
    assert "X-CareerSignal-Namespace" in body


def test_the_message_is_sent_as_bytes_and_never_as_a_path():
    """The browser reads the file it was given; the server resolves no names.

    A path would be an instruction to open something, and a surface that opened what a page
    named would be reading the operator's disk on a page's say-so.
    """
    screens = code(STATIC / "screens.js")
    assert "arrayBuffer()" in screens, "the chosen file is no longer read as bytes"
    chosen = region(screens, "function emlControls(", "\nfunction gmailControls(")
    # The filename is a convenience in the picker and is never read, sent or stored: it is a
    # value the operator's filesystem supplied, and provenance is what they declared instead.
    assert not re.search(r"\.name\b", chosen), "the chosen file's name is read"
    for named in ("webkitRelativePath", "FileReader", "readAsText", "chooser.value"):
        assert named not in chosen, named
    # And no path reaches the request: the namespace and the bytes are the whole of it.
    assert "actions.importEml(namespace.value, await chosen.arrayBuffer())" in chosen


def test_the_browser_parses_nothing_it_uploads():
    """No parser, no preview, no inspection of what is inside the message.

    The intake UI is not an email client. Deciding whether a message looks like a job is the
    extractor's answer, and a preview is the one place recruiter markup would find a parser on
    the origin that holds the launch credential.
    """
    screens = code(STATIC / "screens.js")
    section = region(screens, "function emlControls(", "\nfunction gmailControls(")
    for absent in ("DOMParser", "TextDecoder", "atob", "decodeURIComponent", "split(", "match("):
        assert absent not in section, f"the intake control reaches {absent}"
    for absent in ("iframe", "preview", "innerHTML", "srcdoc"):
        assert absent not in screens.lower(), f"screens.js names {absent}"


def test_the_intake_controls_are_offered_only_where_the_server_says_they_can_operate():
    """Decided by the discovery answer alone.

    Inferring Gmail intake from anything else -- a compose credential, a read token in the
    session facts, a declared mailbox -- would offer a control backed by a different grant, or
    by no grant at all.
    """
    screens = code(STATIC / "screens.js")
    section = region(screens, "export function intakeSection(", "\nfunction filtered(")
    assert "state.sources.eml.available" in section
    assert "state.sources.gmail.available" in section
    for inferred in ("outward", "gmail_token", "gmail_compose_token", "target"):
        assert inferred not in section, f"the intake controls infer availability from {inferred}"
    # A launch with no profile is told so rather than shown controls that must refuse.
    assert "!state.sources" in section


def test_an_unavailable_gmail_source_is_explained_rather_than_offered():
    screens = code(STATIC / "screens.js")
    copy = region(screens, "const INTAKE_COPY = {", "\n};")
    assert "gmailAbsent:" in copy
    absent = region(copy, "gmailAbsent:", "profileLabel:")
    assert "CAREERSIGNAL_GMAIL_TOKEN" in absent
    # And it says plainly that the compose credential is a different grant, because an
    # operator who has one will otherwise assume intake should already work.
    assert "different grant" in absent


@pytest.mark.parametrize("command", ["importEml", "importGmail"])
def test_the_browser_re_reads_after_taking_material_in(command):
    """New evidence moves more than a decision does, so more is re-read.

    A message can create an opportunity, produce a new current review and make an approval
    stale. Which of those happened is the engine's answer; a browser that patched its own rows
    would be deciding it a second time, differently.
    """
    body = code(STATIC / "app.js")
    section = region(body, f"  {command}(", "\n  },")
    assert "await reloadAll()" in section, "intake no longer re-reads"
    assert "state.intake = {" in section
    for patched in ("state.opportunities[", "state.communications.push", "row.presentation"):
        assert patched not in section, f"intake patches {patched} locally"
    # And reloadAll re-reads the Inbox as well as everything reload() already covered.
    whole = region(body, "async function reloadAll()", "\n}")
    assert "api.communications()" in whole and "await reload()" in whole


def test_nothing_retries_an_import():
    """A repeat has to be the operator pressing the button again."""
    body = code(STATIC / "app.js")
    for command in ("importEml", "importGmail"):
        section = region(body, f"  {command}(", "\n  },")
        for absent in ("setTimeout", "setInterval", "while", "for ("):
            assert absent not in section, f"{command} contains {absent}"
        assert section.count("api.ingest") == 1, f"{command} sends more than once"


def test_the_browser_never_names_the_profile_it_is_judged_against():
    """No script assembles skills, locations or a threshold into a request.

    There is no request shape with a place for one, and the way to keep it that way is for no
    script to spell the fields at all.
    """
    for path in SCRIPTS:
        body = without_declarations(code(path))
        for field in ("skills:", "locations:", "threshold", "coverage:", "eligibility"):
            assert field not in body, f"{path.name} assembles {field}"
    # The panel displays the launch profile it was told about, and that is a read.
    screens = code(STATIC / "screens.js")
    assert "state.sources.profile.skills" in screens


def test_the_intake_report_repeats_the_servers_words_rather_than_judging_them():
    """Counts, ids and diagnostics are facts the server reported.

    A browser that summarised them -- "looks like 2 good matches" -- would be a second
    extractor with a gentler vocabulary, and an operator has no way to tell a summary from a
    result.
    """
    screens = code(STATIC / "screens.js")
    report = region(screens, "function intakeReport(", "\nexport function intakeSection(")
    for field in ("answer.messages", "one.reviews", "found.reason", "answer.read"):
        assert field in report, field
    for judged in ("advances", "eligible", "score", "coverage", "matched"):
        assert judged not in report, f"the report decides {judged}"


# Read from the modules that own them rather than spelled out here, so a renamed variable
# cannot leave this check quietly looking for a name nothing uses any more.
CREDENTIALS = (TOKEN_VARIABLE, COMPOSE_TOKEN_VARIABLE, "Bearer ", "ya29.", "access" + "_token")


def test_the_page_does_not_claim_to_be_read_only():
    """The empty detail pane greeted the operator with a claim this surface outgrew.

    It said approving, recording a status and creating a draft stay on the command line, which
    stopped being true in PR 7 and PR 8 and now sits beside an Import control. Copy an operator
    reads is a claim CareerSignal is making, and a false one teaches them to distrust the rest.
    """
    body = code(STATIC / "screens.js")
    for claimed in ("only reads", "read-only surface", "stay on the command line"):
        assert claimed not in body, f"the page claims: {claimed}"
    # What is still true, and is the one promise this product is built around.
    assert "Nothing here is ever sent" in body


def test_no_shipped_asset_carries_anything_that_looks_like_a_credential():
    """The page is served to a browser, so anything compiled into it is on disk, in the wheel,
    and in every copy of the repository.

    The one permitted mention is the read variable's *name*, in the sentence that tells an
    operator which credential a launch is missing. A name is not a value, it is already in the
    documentation, and withholding it would leave the operator with a control that is absent
    and no way to find out why.
    """
    for path in sorted(STATIC.iterdir()):
        body = path.read_text(encoding="utf-8")
        for named in CREDENTIALS:
            if path.name == "screens.js" and named == TOKEN_VARIABLE:
                continue
            assert named not in body, f"{path.name} names {named}"


def test_the_packet_names_a_destination_rather_than_publishing_a_missing_one():
    """Two fields that are null until a decision exists, never concatenated into the page.

    `action.provider` and `action.provider_namespace` are what a *recorded* decision bound.
    Before one exists the API reports both as null -- correctly, because nothing is bound --
    and `null + " . " + null` is the string "null . null", published in the one pane whose
    stated job is to say what an approval would bind, directly under a note promising the
    destination is named there and beside a button that names it in its own label.

    So the recorded pair is read only when there is a decision to read it from, and an
    undecided packet names the launch destination the session reported instead. Neither is
    computed here: both arrive from the server, which is the rule this file exists to keep.
    """
    screens = code(STATIC / "screens.js")
    detail = region(screens, "function opportunityDetail(", "\nfunction digests(")
    guarded = (
        "const destination = record.action.provider\n"
        '    ? record.action.provider + " · " + record.action.provider_namespace\n'
        "    : target\n"
        '      ? target.provider + " · " + target.provider_namespace\n'
        '      : "—";'
    )
    assert guarded in detail, "the destination row is no longer guarded by a recorded decision"
    assert '["destination", destination]' in detail
    # The unguarded form, which is what published "null · null". Named here so that
    # reintroducing it fails rather than merely looking different from the block above.
    unguarded = '["destination", record.action.provider + '
    assert unguarded not in screens, "the destination row reads the pair without a guard"
    # The launch destination is the session's, not a value assembled in the browser.
    assert "const target = extra.target;" in detail
