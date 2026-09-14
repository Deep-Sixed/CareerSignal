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

import importlib.util
import json
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
