"""The publication gate's two reviewed allowances, and what they must still refuse.

`ops/tools/verify_tree.py` decides what may be published. Two of its rules are deliberate
relaxations rather than blanket permissions, and a relaxation that is wider than it was
argued for is the failure worth testing against:

  * a synthetic email domain is RFC 2606 reserved, which includes subdomains of the
    reserved names and the reserved `.example` top-level domain -- not only the three
    apex names the rule originally listed;
  * a binary artifact is refused everywhere except a real PNG in one exact directory of
    design renders -- the name alone is never the evidence.

`ops/tools/verify_secrets.py` carries a third: one detector, in three named artifacts.
Its paths arrive from detect-secrets spelled the way the running platform spells them,
so separator normalisation is part of the comparison and is pinned here too -- as
normalisation only, never as a fourth way to widen the allowance.

All are exercised as pure predicates and again end to end, against a temporary tree for
the tree gate and a findings payload for the secret gate, so a future edit that widens
any rule fails here rather than in review.
"""

import ast
import importlib.util
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

TOOL = Path(__file__).resolve().parents[3] / "ops" / "tools" / "verify_tree.py"


def load():
    """Load the gate as a module. It is a script, not an installed package."""
    spec = importlib.util.spec_from_file_location("verify_tree", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate():
    return load()


# A real PNG signature followed by bytes that are not valid UTF-8, which is the pair of
# facts the gate tests: undecodable as text, and genuinely a PNG.
BINARY = b"\x89PNG\r\n\x1a\n\xff\xfe\x00binary"


@pytest.mark.parametrize(
    "domain",
    [
        "example.com",
        "example.net",
        "example.org",
        "jobs.example.com",
        "lists.example.org",
        "mail.alerts.example.net",
        "fabrikam.example",
        "careers.contoso.example",
        "users.noreply.github.com",
        "EXAMPLE.COM",
        "Jobs.Example.Com",
        "example.com.",
    ],
)
def test_reserved_domains_are_synthetic(gate, domain):
    assert gate.synthetic_domain(domain)


@pytest.mark.parametrize(
    "domain",
    [
        "gmail.com",
        "googlemail.com",
        # Ends with the reserved name as a substring, but is a different registration.
        "notexample.com",
        "badexample.org",
        # Contains a reserved name without being under it.
        "example.com.attacker.net",
        "example.example.com.evil.io",
        # Reserved label in the wrong position: .example must be the top-level domain.
        "example.co",
        "exampled.com",
        "github.com",
    ],
)
def test_real_domains_are_not_synthetic(gate, domain):
    assert not gate.synthetic_domain(domain)


def written(root, name, payload):
    """Place one file and return the (relative, absolute) pair the gate is asked about."""
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return Path(name), path


@pytest.mark.parametrize(
    "name",
    ["docs/ui-design/renders/dashboard.png", "docs/ui-design/renders/INBOX.PNG"],
)
def test_design_renders_are_reviewed(gate, tmp_path, name):
    assert gate.reviewed_render(*written(tmp_path, name, BINARY))


@pytest.mark.parametrize(
    "name",
    [
        # Right suffix, wrong place. A PNG is reviewed because of where it is, not what it is.
        "docs/renders/dashboard.png",
        "docs/ui-design/dashboard.png",
        "renders/dashboard.png",
        "src/system/renders/dashboard.png",
        "docs/ui-design/renders/nested/dashboard.png",
        # Right place, wrong artifact.
        "docs/ui-design/renders/screens.zip",
        "docs/ui-design/renders/mock.pdf",
        "docs/ui-design/renders/private.db",
    ],
)
def test_everything_else_is_not_a_reviewed_render(gate, tmp_path, name):
    # Written with a genuine PNG signature, so the refusal is about the path and nothing else.
    assert not gate.reviewed_render(*written(tmp_path, name, BINARY))


@pytest.mark.parametrize(
    "payload",
    [
        # A JPEG, a PDF, a zip and an ELF binary, each renamed to .png.
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00",
        b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n",
        b"PK\x03\x04\x14\x00\x00\x00\x08\x00",
        b"\x7fELF\x02\x01\x01\x00\x00",
        # Truncated: the right first bytes, but not the whole signature.
        b"\x89PNG\r\n",
        b"",
    ],
)
def test_a_renamed_binary_is_not_a_reviewed_render(gate, tmp_path, payload):
    relative, path = written(tmp_path, "docs/ui-design/renders/dashboard.png", payload)
    assert not gate.reviewed_render(relative, path)


def test_a_render_that_cannot_be_read_is_not_reviewed(gate, tmp_path):
    """A path with no file behind it is refused rather than raising out of the gate."""
    assert not gate.reviewed_render(
        Path("docs/ui-design/renders/dashboard.png"), tmp_path / "absent.png"
    )


def tree(root, files):
    for name, payload in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)


def inspect(gate, root, monkeypatch):
    """Run the real gate against a temporary tree and return its refusals."""
    monkeypatch.setattr(gate, "ROOT", root)
    try:
        gate.inspect()
    except SystemExit as exit:
        return str(exit)
    return ""


def test_reviewed_render_publishes_and_is_counted(gate, tmp_path, monkeypatch, capsys):
    tree(tmp_path, {"docs/ui-design/renders/dashboard.png": BINARY})
    assert inspect(gate, tmp_path, monkeypatch) == ""
    assert "1 reviewed renders" in capsys.readouterr().out


def test_binary_outside_the_render_directory_is_still_refused(gate, tmp_path, monkeypatch):
    tree(tmp_path, {"docs/ui-design/dashboard.png": BINARY})
    assert "Non-text artifact requires review" in inspect(gate, tmp_path, monkeypatch)


def test_other_binary_inside_the_render_directory_is_still_refused(gate, tmp_path, monkeypatch):
    tree(tmp_path, {"docs/ui-design/renders/screens.zip": BINARY})
    assert "Private/binary artifact requires review" in inspect(gate, tmp_path, monkeypatch)


def test_renamed_binary_in_the_render_directory_is_still_refused(gate, tmp_path, monkeypatch):
    tree(tmp_path, {"docs/ui-design/renders/dashboard.png": b"\xff\xd8\xff\xe0JFIF\x00\xfe"})
    assert "Non-text artifact requires review" in inspect(gate, tmp_path, monkeypatch)


def test_reserved_subdomain_publishes(gate, tmp_path, monkeypatch):
    tree(tmp_path, {"docs/fixture.md": b"alerts@jobs.example.com and hr@fabrikam.example\n"})
    assert inspect(gate, tmp_path, monkeypatch) == ""


def test_deliverable_address_is_still_refused(gate, tmp_path, monkeypatch):
    # Assembled rather than written out: the gate under test scans every publishable file,
    # this one included, so a literal deliverable address here would refuse the whole tree.
    deliverable = ("someone@" + "gmail.com\n").encode()
    tree(tmp_path, {"docs/fixture.md": deliverable})
    assert "Non-example email requires review" in inspect(gate, tmp_path, monkeypatch)


# ── the secret scanner's one reviewed allowance ──────────────────────────────────────
SECRETS_TOOL = Path(__file__).resolve().parents[3] / "ops" / "tools" / "verify_secrets.py"


@pytest.fixture
def scanner():
    """Load the secret gate. It imports verify_tree by name, so that path is primed first."""
    spec = importlib.util.spec_from_file_location("verify_secrets", SECRETS_TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("verify_tree", load())
    spec.loader.exec_module(module)
    return module


HEX = "Hex High Entropy String"


@pytest.mark.parametrize(
    "path",
    [
        "docs/ui-design/CareerSignal-Mock.dc.html",
        "docs/ui-design/CareerSignal-Handoff.dc.html",
        "docs/ui-design/CareerSignal-UI-Review.dc.html",
    ],
)
def test_synthetic_identifiers_in_frozen_artifacts_are_reviewed(scanner, path):
    assert scanner.reviewed_design_finding(path, HEX)


@pytest.mark.parametrize(
    ("path", "detector"),
    [
        # Another detector in the same reviewed file is not covered by this allowance.
        ("docs/ui-design/CareerSignal-Mock.dc.html", "Base64 High Entropy String"),
        ("docs/ui-design/CareerSignal-Mock.dc.html", "AWS Access Key"),
        ("docs/ui-design/CareerSignal-Mock.dc.html", "Private Key"),
        # The right detector in the wrong place.
        ("docs/ui-design/renders/dashboard.png", HEX),
        ("docs/ui-design/nested/CareerSignal-Mock.dc.html", HEX),
        ("docs/CareerSignal-Mock.dc.html", HEX),
        ("src/system/cli.py", HEX),
        ("README.md", HEX),
        # A suffix that merely ends in .html is not a design export.
        ("docs/ui-design/notes.html", HEX),
        # The allowance names three reviewed artifacts, not a shape. A fourth export
        # dropped beside them inherits nothing until someone adds it deliberately.
        ("docs/ui-design/CareerSignal-Sketch.dc.html", HEX),
        ("docs/ui-design/CareerSignal-Mock-v2.dc.html", HEX),
        ("docs/ui-design/CareerSignal-Mock.dc.html.bak", HEX),
        ("docs/ui-design/careersignal-mock.dc.html", HEX),
    ],
)
def test_everything_else_still_stops_the_secret_gate(scanner, path, detector):
    assert not scanner.reviewed_design_finding(path, detector)


def scan_result(scanner, monkeypatch, results):
    """Drive main()'s triage over a findings payload, without re-running the detector.

    The detector's own behaviour is not what this proves -- whether a given string trips
    HexHighEntropyString depends on entropy heuristics and on scan context, and pinning
    that here would be testing detect-secrets rather than CareerSignal. What must hold is
    the triage: of the findings that come back, exactly the reviewed ones are allowed
    through and everything else still stops the gate.
    """
    payload = json.dumps({"results": results})
    monkeypatch.setattr(
        scanner.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(stdout=payload, stderr="", returncode=0),
    )
    monkeypatch.setattr(scanner, "files", lambda: iter(()))
    try:
        scanner.main()
    except SystemExit as exit:
        return str(exit)
    return ""


MOCK = "docs/ui-design/CareerSignal-Mock.dc.html"


def test_reviewed_finding_passes_and_is_counted(scanner, monkeypatch, capsys):
    results = {MOCK: [{"type": HEX, "line_number": 467}]}
    assert scan_result(scanner, monkeypatch, results) == ""
    assert "1 reviewed synthetic identifiers" in capsys.readouterr().out


def test_other_detector_in_a_reviewed_artifact_still_stops_the_gate(scanner, monkeypatch):
    results = {MOCK: [{"type": "Base64 High Entropy String", "line_number": 12}]}
    assert "Secret scan requires review" in scan_result(scanner, monkeypatch, results)


def test_same_finding_in_another_file_still_stops_the_gate(scanner, monkeypatch):
    results = {"docs/notes.md": [{"type": HEX, "line_number": 3}]}
    assert "Secret scan requires review" in scan_result(scanner, monkeypatch, results)


def test_a_refused_finding_is_not_masked_by_a_reviewed_one(scanner, monkeypatch, capsys):
    """One allowed finding beside one refused finding must still refuse, and say which."""
    results = {
        MOCK: [{"type": HEX, "line_number": 467}, {"type": "Private Key", "line_number": 9}],
    }
    assert "Secret scan requires review" in scan_result(scanner, monkeypatch, results)
    printed = capsys.readouterr().out
    assert "Private Key" in printed and HEX not in printed


# ── separator normalisation: the same three artifacts, either platform's spelling ────
# detect-secrets reports the path as the operating system spells it, so a Windows runner
# names the mock with backslashes. Before the fix those never matched the allowlist and
# all four Windows cells refused a reviewed artifact -- caught by a durability push, not
# by review, which is why both spellings are pinned here.
WINDOWS_ARTIFACTS = [
    r"docs\ui-design\CareerSignal-Mock.dc.html",
    r"docs\ui-design\CareerSignal-Handoff.dc.html",
    r"docs\ui-design\CareerSignal-UI-Review.dc.html",
]


@pytest.mark.parametrize("path", WINDOWS_ARTIFACTS)
def test_windows_spelling_resolves_to_the_same_reviewed_artifact(scanner, path):
    assert scanner.reviewed_design_finding(path, HEX)


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        (r"docs\ui-design\CareerSignal-Mock.dc.html", MOCK),
        (
            r"docs\ui-design\CareerSignal-Handoff.dc.html",
            "docs/ui-design/CareerSignal-Handoff.dc.html",
        ),
        (
            r"docs\ui-design\CareerSignal-UI-Review.dc.html",
            "docs/ui-design/CareerSignal-UI-Review.dc.html",
        ),
        # A mixed spelling normalises the same way rather than becoming a third form.
        (r"docs/ui-design\CareerSignal-Mock.dc.html", MOCK),
        # Already canonical: normalising is idempotent.
        (MOCK, MOCK),
    ],
)
def test_both_spellings_canonicalise_identically(scanner, reported, expected):
    assert scanner.canonical_path(reported) == expected


@pytest.mark.parametrize(
    "path",
    [
        # The posix near misses, spelled the Windows way. Folding separators must not be
        # an occasion to admit anything the allowlist excludes.
        r"docs\ui-design\CareerSignal-Mock-v2.dc.html",
        r"docs\ui-design\CareerSignal-Sketch.dc.html",
        r"docs\ui-design\CareerSignal-Mock.dc.html.bak",
        r"docs\CareerSignal-Mock.dc.html",
        r"docs\ui-design\nested\CareerSignal-Mock.dc.html",
        r"docs\ui-design\renders\dashboard.png",
        r"src\system\cli.py",
    ],
)
def test_windows_near_misses_are_still_refused(scanner, path):
    assert not scanner.reviewed_design_finding(path, HEX)


@pytest.mark.parametrize(
    "path",
    [
        # Windows is case-insensitive about filenames; this gate is not. The allowlist
        # names three exact artifacts, and folding case would admit a fourth file.
        r"docs\ui-design\careersignal-Mock.dc.html",
        r"docs\UI-Design\CareerSignal-Mock.dc.html",
        r"DOCS\ui-design\CareerSignal-Mock.dc.html",
        "docs/ui-design/careersignal-mock.dc.html",
    ],
)
def test_case_is_not_folded_by_normalisation(scanner, path):
    assert not scanner.reviewed_design_finding(path, HEX)


@pytest.mark.parametrize(
    "path",
    [
        # Walking out and back in is not the same path as never leaving: `..` is left in
        # place rather than resolved, so these compare unequal and are refused.
        "docs/ui-design/../ui-design/CareerSignal-Mock.dc.html",
        r"docs\ui-design\..\ui-design\CareerSignal-Mock.dc.html",
        # An absolute path from a runner is not the relative path the allowlist names.
        r"D:\a\CareerSignal\CareerSignal\docs\ui-design\CareerSignal-Mock.dc.html",
        "/home/runner/work/CareerSignal/docs/ui-design/CareerSignal-Mock.dc.html",
    ],
)
def test_normalisation_does_not_resolve_or_absolutise(scanner, path):
    assert not scanner.reviewed_design_finding(path, HEX)


@pytest.mark.parametrize(
    "detector", ["Base64 High Entropy String", "Private Key", "AWS Access Key"]
)
def test_windows_spelling_does_not_widen_the_detector(scanner, detector):
    """The fix is about separators. Every other detector still stops the gate."""
    assert not scanner.reviewed_design_finding(WINDOWS_ARTIFACTS[0], detector)


def test_windows_reported_finding_passes_triage(scanner, monkeypatch, capsys):
    """End to end through main(): the Windows spelling of the mock is allowed through."""
    results = {WINDOWS_ARTIFACTS[0]: [{"type": HEX, "line_number": 467}]}
    assert scan_result(scanner, monkeypatch, results) == ""
    assert "1 reviewed synthetic identifiers" in capsys.readouterr().out


def test_windows_spelled_refusal_still_stops_the_gate(scanner, monkeypatch):
    results = {r"docs\ui-design\CareerSignal-Mock-v2.dc.html": [{"type": HEX, "line_number": 5}]}
    assert "Secret scan requires review" in scan_result(scanner, monkeypatch, results)


# --- the documented web capability manifest -----------------------------------------------------
#
# The third contract in this file, and the same shape as the two above: a claim that is
# argued for in prose, pinned mechanically so a later edit that widens it fails here rather
# than in review.
#
# Three PRs in a row shipped a capability and left a claim about it behind somewhere, because
# a capability lands in one file and is described in several. The answer is not to duplicate a
# machine-readable list into each of them -- that trades prose drift for manifest drift. It is
# to name one canonical declaration, in the document that already explains what these commands
# are, and compare it against what the code can actually do.


SOURCE = Path(__file__).resolve().parents[2]
WEB = SOURCE / "system" / "web"
SURFACE = Path(__file__).resolve().parents[3] / "docs" / "web-surface.md"
MANIFEST = re.compile(r"<!--[ \t]*careersignal-web-capabilities[ \t]*\n(.*?)^-->", re.S | re.M)


def declared(text) -> set:
    """The capabilities docs/web-surface.md declares, as a set.

    Refuses rather than guesses. A block that is missing, duplicated, or carries anything but
    one bare identifier per line is a broken declaration, and a broken declaration must not
    quietly become an empty one -- an empty set would compare equal to a package that had lost
    its capabilities, which is exactly the wrong direction to fail in.
    """
    blocks = MANIFEST.findall(text)
    if not blocks:
        raise ValueError("No careersignal-web-capabilities block")
    if len(blocks) > 1:
        raise ValueError(f"{len(blocks)} careersignal-web-capabilities blocks; there may be one")
    names = [line.strip() for line in blocks[0].splitlines() if line.strip()]
    if not names:
        raise ValueError("The capability block is empty")
    for name in names:
        if not name.isidentifier():
            raise ValueError(f"Not a capability name: {name!r}")
    if len(set(names)) != len(names):
        raise ValueError("The capability block repeats a name")
    return set(names)


def _parsed(root, skip):
    for path in sorted(root.rglob("*.py")):
        if "tests" in path.parts or skip in path.parents or path == skip:
            continue
        yield ast.parse(path.read_text(encoding="utf-8"))


def _calls(node) -> set:
    """Every method name this function calls on something -- `x.claim(...)` gives `claim`."""
    return {
        call.func.attr
        for call in ast.walk(node)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
    }


def entrypoints(source=SOURCE, web=WEB) -> set:
    """Every name a caller can invoke that ends in a database write.

    Seeded from the functions that open `transaction(...)` and closed over calls, so a
    method is an entrypoint when it *reaches* a write, not only when it performs one. That
    matters because the outward boundary is a service: `OutwardActions.draft()` writes
    through `claim`, `finish`, `refuse` and `reject` without the caller naming any of them,
    and a derivation that only looked for repository members would let a web surface acquire
    the whole outward workflow while still declaring nothing.

    The web package itself is excluded from the scan. Its own helpers are not capabilities --
    they are how it spends the ones it has, and counting them would ask the manifest to
    declare an implementation detail.
    """
    functions = {}
    for tree in _parsed(source, web):
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                functions.setdefault(node.name, []).append(node)
    assert functions, "no source was parsed; this guard has stopped guarding anything"

    def opens(node) -> bool:
        return any(
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == "transaction"
            for call in ast.walk(node)
        )

    writing = {name for name, nodes in functions.items() if any(opens(node) for node in nodes)}
    assert writing, "nothing opens a transaction; the seed for this closure is empty"
    while True:
        grown = writing | {
            name
            for name, nodes in functions.items()
            if any(_calls(node) & writing for node in nodes)
        }
        if grown == writing:
            return writing
        writing = grown


def named(web=WEB) -> set:
    """Every member name the web package invokes, on anything.

    Deliberately not restricted to a variable called `repository`. What matters is which
    write entrypoint is reached, not which local name it was reached through -- and the
    service that owns the outward boundary is reached through a variable called something
    else entirely.
    """
    modules = sorted(web.rglob("*.py"))
    assert modules, "the web package moved and this guard stopped guarding anything"
    found = set()
    for path in modules:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute):
                found.add(node.attr)
    return found


def capabilities(source=SOURCE, web=WEB) -> set:
    """What the browser can actually change: the write entrypoints this package invokes."""
    return named(web) & entrypoints(source, web)


def test_the_documented_capabilities_are_the_ones_the_web_package_can_reach():
    """The contract, in one line, compared in both directions.

    A capability the code gains without being declared fails here, and a capability declared
    without the code being able to reach it fails here too. Either way the failure lands in
    the change that caused it rather than three PRs later.
    """
    assert declared(SURFACE.read_text(encoding="utf-8")) == capabilities()


def test_the_manifest_is_not_vacuous():
    """Stated separately, because an equality of two empty sets would also pass.

    If the derivation ever stopped finding anything -- a moved package, a renamed helper --
    the test above would go green while proving nothing at all.
    """
    assert capabilities() == {"record_status", "decide"}
    assert {"claim", "finish", "draft", "reconcile"} & capabilities() == set()


def test_every_declared_capability_is_a_real_write_entrypoint():
    """A typo is not a capability. `recordstatus` names nothing and must not read as nothing."""
    assert declared(SURFACE.read_text(encoding="utf-8")) <= entrypoints()


def test_the_outward_workflow_is_an_entrypoint_even_though_it_writes_indirectly():
    """The derivation has to follow the service, not only the repository.

    `OutwardActions.draft()` and `.reconcile()` reach `claim`, `finish`, `refuse` and
    `reject` without their caller naming any of them. If the closure stopped at repository
    members, a web surface could acquire the entire outward workflow and still declare
    nothing -- which is exactly the change this guard exists to force.
    """
    reachable = entrypoints()
    assert {"draft", "reconcile"} <= reachable
    assert {"claim", "finish", "refuse", "reject", "record_status", "decide"} <= reachable
    # And they are entrypoints for the right reason: the service is where they are reached.
    assert {"intake", "intake_message"} <= reachable


def test_the_declaration_is_a_set_rather_than_a_formatting_convention():
    """Order is not part of the contract, and neither is surrounding blank space."""
    block = "<!-- careersignal-web-capabilities\n{}\n-->"
    assert declared(block.format("decide\nrecord_status")) == {"record_status", "decide"}
    assert declared(block.format("  record_status  \n\n\tdecide\t")) == {"record_status", "decide"}


@pytest.mark.parametrize(
    "broken",
    (
        "nothing here at all",
        "<!-- careersignal-web-capabilities\n-->",
        "<!-- careersignal-web-capabilities\n\n  \n-->",
        "<!-- careersignal-web-capabilities\nrecord_status\nrecord_status\n-->",
        "<!-- careersignal-web-capabilities\nrecord status\n-->",
        "<!-- careersignal-web-capabilities\nrecord_status()\n-->",
        "<!-- careersignal-web-capabilities\n- record_status\n-->",
        "<!-- careersignal-web-capabilities\ndecide\n-->\n<!-- careersignal-web-capabilities\ndecide\n-->",
    ),
)
def test_a_broken_declaration_is_refused_rather_than_read_as_empty(broken):
    with pytest.raises(ValueError):
        declared(broken)


def test_the_frozen_design_baseline_declares_nothing():
    """`docs/ui-design/` is the accepted design as accepted, and is never edited.

    It must not acquire a manifest, and this guard must never read one from it: the canonical
    declaration is one document, and a second copy anywhere is the drift this exists to stop.
    """
    frozen = Path(__file__).resolve().parents[3] / "docs" / "ui-design"
    for path in sorted(frozen.rglob("*")):
        if path.is_file():
            assert not MANIFEST.findall(path.read_text(encoding="utf-8", errors="replace"))


def test_exactly_one_document_carries_the_declaration():
    """One canonical place, mechanically. A second block anywhere is a second source of truth."""
    root = Path(__file__).resolve().parents[3]
    carrying = [
        path
        for path in sorted(root.rglob("*.md"))
        if ".git" not in path.parts and MANIFEST.findall(path.read_text(encoding="utf-8"))
    ]
    assert carrying == [SURFACE]


# --- the shape PR 8 will actually take ----------------------------------------------------------


def outward_tree(root) -> tuple:
    """A miniature of this repository's real layering, for the one case that matters.

    A repository whose write opens a transaction; a service that reaches that write on the
    caller's behalf; and a web package that uses the service, never the repository. That is
    how the outward boundary is built here, and it is the arrangement a derivation restricted
    to repository members cannot see through.
    """
    source = root / "src"
    web = source / "system" / "web"
    web.mkdir(parents=True)
    (source / "data").mkdir(parents=True)
    (source / "data" / "repository.py").write_text(
        "class Repository:\n"
        "    def claim(self, review):\n"
        "        with connection(self.path) as conn, transaction(conn):\n"
        "            conn.execute('UPDATE draft_intents SET state=?', ('claimed',))\n"
        "\n"
        "    def opportunities(self):\n"
        "        with connection(self.path) as conn:\n"
        "            return conn.execute('SELECT 1').fetchall()\n",
        encoding="utf-8",
    )
    (source / "system" / "workflow.py").write_text(
        "class OutwardActions:\n"
        "    def __init__(self, repository, provider):\n"
        "        self.repository, self.provider = repository, provider\n"
        "\n"
        "    def draft(self, review):\n"
        "        self.provider.create(review)\n"
        "        return self.repository.claim(review)\n",
        encoding="utf-8",
    )
    return source, web


def test_a_web_surface_that_drafts_through_the_service_is_not_invisible(tmp_path):
    """The blocker this guard exists for, proved on the shape PR 8 will use.

    The web package here names no repository write at all -- it calls `actions.draft(...)`,
    exactly as the real outward path should be written. A derivation that looked only for
    members of a variable called `repository` would report no new capability, leave the
    manifest agreeing with itself, and stay green while the browser gained the power to
    create drafts. This asserts the opposite: the capability appears, and the old manifest
    stops matching until it is updated.
    """
    source, web = outward_tree(tmp_path)
    (web / "server.py").write_text(
        "from system.workflow import OutwardActions\n"
        "\n"
        "def _draft(self, review):\n"
        "    actions = OutwardActions(self.server.repository, self.server.provider)\n"
        "    return actions.draft(review)\n",
        encoding="utf-8",
    )
    assert "repository.claim" not in (web / "server.py").read_text(encoding="utf-8")

    gained = capabilities(source, web)
    assert gained == {"draft"}

    stale = "<!-- careersignal-web-capabilities\nrecord_status\ndecide\n-->"
    assert declared(stale) != gained, "the stale manifest still matched; the guard is blind"
    updated = "<!-- careersignal-web-capabilities\ndraft\n-->"
    assert declared(updated) == gained


def test_reconciling_through_the_service_is_counted_the_same_way(tmp_path):
    """The second outward command, which reaches only `finish`."""
    source, web = outward_tree(tmp_path)
    (source / "system" / "workflow.py").write_text(
        "class OutwardActions:\n"
        "    def __init__(self, repository, provider):\n"
        "        self.repository, self.provider = repository, provider\n"
        "\n"
        "    def reconcile(self, review):\n"
        "        found = self.provider.lookup(review)\n"
        "        return self.repository.claim(found)\n",
        encoding="utf-8",
    )
    (web / "server.py").write_text(
        "def _reconcile(self, review):\n    return self.server.actions.reconcile(review)\n",
        encoding="utf-8",
    )
    assert capabilities(source, web) == {"reconcile"}


def test_a_web_surface_that_reimplements_the_workflow_is_also_counted(tmp_path):
    """The other route to the same power, closed by the same rule.

    A surface that skipped the service and wrote the workflow itself would name the
    repository's own writes. Both routes have to be visible, or the guard would merely
    choose which way round it can be defeated.
    """
    source, web = outward_tree(tmp_path)
    (web / "server.py").write_text(
        "def _draft(self, review):\n    return self.server.repository.claim(review)\n",
        encoding="utf-8",
    )
    assert capabilities(source, web) == {"claim"}


def test_a_web_surface_that_only_reads_declares_nothing(tmp_path):
    """The floor. A read is not a capability, however it is reached."""
    source, web = outward_tree(tmp_path)
    (web / "server.py").write_text(
        "def _list(self):\n    return self.server.repository.opportunities()\n",
        encoding="utf-8",
    )
    assert capabilities(source, web) == set()
