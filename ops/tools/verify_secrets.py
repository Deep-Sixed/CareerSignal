"""Run the pinned secret detector on the same publishable file set as the tree audit."""

import json
import subprocess
import sys
from pathlib import PurePosixPath

from verify_tree import ROOT, files

# The frozen UI design artifacts carry synthetic 64-character identifiers, generated in the
# mock by a hash helper so the screens show ids shaped like the engine's. detect-secrets
# reads them as high-entropy hex, correctly -- they look exactly like what it hunts for.
# They cannot be annotated the way src/recruiting/tests/test_rules.py annotates its one
# synthetic credential URL, because the artifacts are frozen design exports and editing
# them would make the committed file differ from what Design accepted.
#
# The allowance is therefore narrow in three directions at once: one directory, one suffix,
# and one detector. A Base64 finding, an AWS key, a private key header, or anything at all
# in another file still stops the gate -- including in these same artifacts.
REVIEWED_DESIGN_DIRECTORY = ("docs", "ui-design")
REVIEWED_DESIGN_SUFFIX = ".dc.html"
REVIEWED_DESIGN_DETECTOR = "Hex High Entropy String"


def reviewed_design_finding(path: str, detector: str) -> bool:
    """Whether one finding is a known-synthetic identifier in a frozen design artifact."""
    parts = tuple(PurePosixPath(path).parts)
    return (
        detector == REVIEWED_DESIGN_DETECTOR
        and len(parts) == len(REVIEWED_DESIGN_DIRECTORY) + 1
        and parts[: len(REVIEWED_DESIGN_DIRECTORY)] == REVIEWED_DESIGN_DIRECTORY
        and parts[-1].endswith(REVIEWED_DESIGN_SUFFIX)
    )


def main():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "detect_secrets",
            "scan",
            "--no-verify",
            "--all-files",
            *[str(p.relative_to(ROOT)) for p in files()],
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=True,
    )
    findings = json.loads(result.stdout)["results"]
    refused, reviewed = {}, 0
    for path, records in findings.items():
        # Locations only, never the matched value -- for the allowed findings as much as
        # for the refused ones, since the point is that neither is echoed.
        unreviewed = [r for r in records if not reviewed_design_finding(path, r["type"])]
        reviewed += len(records) - len(unreviewed)
        if unreviewed:
            refused[path] = unreviewed
    if refused:
        for path, records in refused.items():
            print(path, [(r["type"], r["line_number"]) for r in records])
        raise SystemExit("Secret scan requires review")
    print(
        f"PASS: detect-secrets, all publishable files, network verification disabled; "
        f"{reviewed} reviewed synthetic identifiers in frozen design artifacts"
    )


if __name__ == "__main__":
    main()
