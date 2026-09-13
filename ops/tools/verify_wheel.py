"""Build and verify a wheel in a fresh environment outside the checkout."""

import json
import os
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

STATUS_CHECK = """
from data.repository import Repository
from data.store import connection

repository = Repository("extraction.db")
with connection("extraction.db") as conn:
    first = conn.execute("SELECT id FROM opportunities ORDER BY id").fetchall()[0][0]

repository.record_status(first, "Applied", actor="wheel-operator", reason="installed check")
assert repository.status(first) == "applied"

reopened = Repository("extraction.db")
assert reopened.status(first) == "applied"
assert [row[0] for row in reopened.status_history(first)] == ["new", "applied"]

try:
    reopened.record_status(first, "reopened", actor="wheel-operator")
except ValueError:
    pass
else:
    raise AssertionError("installed wheel accepted a status outside the vocabulary")

with connection("extraction.db") as conn:
    for sql in ("DELETE FROM opportunity_status_history",
                "UPDATE opportunity_status_history SET status='closed'"):
        try:
            conn.execute(sql)
        except Exception:
            continue
        raise AssertionError(f"installed wheel allowed: {sql}")
print("status history verified from the installed wheel")
"""


def main():
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONHOME"}}
    with tempfile.TemporaryDirectory(prefix="careersignal-wheel-") as tmp:
        folder = Path(tmp)
        subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "--outdir", str(folder)],
            cwd=ROOT,
            env=env,
            check=True,
        )
        wheel = next(folder.glob("*.whl"))
        assert wheel.name.endswith("-py3-none-any.whl"), wheel.name
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            assert "data/migrations/0001_baseline.sql" in names
            assert not any(set(Path(n).parts) & {"tests", "fixtures", "var", "ops"} for n in names)
            metadata_name = next(n for n in names if n.endswith(".dist-info/METADATA"))
            metadata = archive.read(metadata_name).decode("utf-8")
            assert "\nRequires-Dist:" not in metadata

        target = folder / "environment"
        venv.EnvBuilder(with_pip=True).create(target)
        python = target / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

        def run(*args, capture=False):
            return subprocess.run(
                [str(python), "-I", *args],
                cwd=folder,
                env=env,
                check=True,
                text=True,
                capture_output=capture,
            )

        def installed():
            result = run("-m", "pip", "list", "--format=json", capture=True)
            return {row["name"].casefold() for row in json.loads(result.stdout)}

        before = installed()
        run("-m", "pip", "install", "--disable-pip-version-check", "--no-deps", str(wheel))
        after = installed()
        assert after - before == {"careersignal"}, after - before
        run("-m", "pip", "check")
        run(
            "-c",
            "import sysconfig, pathlib, recruiting, communications, data, system; "
            "root=pathlib.Path(sysconfig.get_path('purelib')).resolve(); "
            "assert all(pathlib.Path(m.__file__).resolve().is_relative_to(root) "
            "for m in (recruiting, communications, data, system))",
        )
        run("-m", "system.entrypoint", "version")
        run("-m", "system.cli", "init", "--db", str(folder / "blank.db"))
        run("-m", "system.cli", "verify", "--db", str(folder / "blank.db"))
        run("-m", "system.cli", "demo", "--db", str(folder / "golden.db"))
        run("-m", "system.cli", "demo", "--db", str(folder / "golden.db"))
        raw = (
            "Message-ID: <wheel-synthetic@example.com>\nContent-Type: text/plain; charset=utf-8\n\n"
            "Role: Engineer\nCompany: Example Company\nLocation: remote\nSkills: python\n"
            "URL: https://jobs.example.com/1\nRole: Analyst\nCompany: Example Company\n"
            "URL: https://jobs.example.com/2\n"
        )
        message = folder / "synthetic.eml"
        message.write_text(raw, encoding="utf-8")
        for _ in range(2):
            run(
                "-m",
                "system.cli",
                "ingest",
                "--db",
                str(folder / "extraction.db"),
                "--message",
                str(message),
                "--namespace",
                "wheel-synthetic",
                "--skill",
                "python",
            )
        run(
            "-c",
            "from data.store import connection; "
            "c=connection('extraction.db'); db=c.__enter__(); "
            "assert db.execute('SELECT count(*) FROM opportunities').fetchone()[0]==2; "
            "assert db.execute('SELECT count(*) FROM extraction_items').fetchone()[0]==2; "
            "assert db.execute('SELECT count(*) FROM draft_intents').fetchone()[0]==0; "
            "assert db.execute('SELECT count(*) FROM opportunity_status_history')"
            ".fetchone()[0]==2; "
            "assert db.execute('SELECT DISTINCT status FROM opportunity_status_history')"
            ".fetchall()==[('new',)]; "
            "c.__exit__(None,None,None)",
        )
        check = folder / "status_check.py"
        check.write_text(STATUS_CHECK, encoding="utf-8")
        run(str(check))
    print(
        "PASS: pure-Python installed wheel, zero runtime dependencies, resource discovery, "
        "golden workflow and replay"
    )


if __name__ == "__main__":
    main()
