"""Run the pinned secret detector on the same publishable file set as the tree audit."""

import json
import subprocess
import sys

from verify_tree import ROOT, files


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
    if findings:
        # Print locations only; never echo potentially sensitive values.
        for path, records in findings.items():
            print(path, [(r["type"], r["line_number"]) for r in records])
        raise SystemExit("Secret scan requires review")
    print("PASS: detect-secrets, all publishable files, network verification disabled")


if __name__ == "__main__":
    main()
