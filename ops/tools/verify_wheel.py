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

# The read surface, started from the installed wheel with no checkout anywhere: the static
# assets have to arrive as package resources, a read has to answer with the launch token,
# and a write verb has to be refused. Locating assets beside __file__ passes every test in
# the tree and fails exactly here, which is why this runs against the installed copy.
WEB_CHECK = """
import json
import threading
import urllib.error
import urllib.request
from importlib import resources

from data.repository import Repository
from system.web.server import TOKEN_HEADER, Surface

packaged = sorted(entry.name for entry in (resources.files("system.web") / "static").iterdir())
assert packaged == [
    "api.js",
    "app.js",
    "careersignal.css",
    "dom.js",
    "icon.svg",
    "index.html",
    "screens.js",
], packaged

surface = Surface(Repository("extraction.db"), port=0)
threading.Thread(target=surface.serve_forever, daemon=True).start()


def ask(path, method="GET", token=True, body=None, origin=True):
    request = urllib.request.Request(surface.origin + path, method=method, data=body)
    if token:
        request.add_header(TOKEN_HEADER, surface.token)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    if origin:
        request.add_header("Origin", surface.origin)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as refused:
        return refused.code, refused.read()


assert surface.server_address[0] == "127.0.0.1", surface.server_address
assert surface.launch_url.startswith("http://127.0.0.1:"), surface.launch_url
assert "#token=" in surface.launch_url and "?" not in surface.launch_url

status, body = ask("/api/v1/opportunities")
assert status == 200, (status, body)
assert len(json.loads(body)) == 2, body
assert ask("/")[0] == 200 and b"app.js" in ask("/")[1]
assert ask("/screens.js")[0] == 200 and ask("/dom.js")[0] == 200
assert ask("/icon.svg")[0] == 200
assert ask("/careersignal.css")[0] == 200
assert ask("/api/v1/opportunities", token=False)[0] == 401
assert ask("/nothing-here.js")[0] == 404

before = ask("/api/v1/timeline")[1]
for method in ("POST", "PUT", "PATCH", "DELETE"):
    status, body = ask("/api/v1/opportunities", method=method)
    assert status == 405, (method, status, body)
assert ask("/api/v1/timeline")[1] == before, "a refused write still moved a ledger"

# The one command, from the installed wheel: the vocabulary is served, a status appends,
# and the same command sent twice is refused the second time without writing.
vocabulary = json.loads(ask("/api/v1/statuses")[1])["statuses"]
assert vocabulary[0] == "new" and "interviewing" in vocabulary, vocabulary
listed = json.loads(ask("/api/v1/opportunities")[1])
chosen, event = listed[0]["id"], listed[0]["status_event"]
address = "/api/v1/opportunities/" + chosen + "/status"
order = json.dumps({"status": "applied", "expected_event_id": event}).encode()

status, body = ask(address, method="POST", body=order)
assert status == 200, (status, body)
assert json.loads(body)["status"] == "applied", body

status, body = ask(address, method="POST", body=order)
assert status == 409, (status, body)
assert json.loads(body)["error"] == "status_conflict", body

assert ask(address, method="POST", body=order, origin=False)[0] == 403
assert ask(address, method="POST", body=order, token=False)[0] == 401
assert ask(address, method="POST", body=b"{oops", origin=True)[0] == 400

history = json.loads(ask("/api/v1/opportunities/" + chosen)[1])["history"]
assert [row["status"] for row in history][-1] == "applied", history
assert sum(1 for row in history if row["status"] == "applied") == 1, history
assert history[-1]["actor"] == "operator", history

surface.shutdown()
print("read surface and the status command verified from the installed wheel")
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
            # Read from the tree rather than listed here, so an asset added to the
            # frontend cannot be left out of the wheel and out of this check at once.
            static = ROOT / "src/system/web/static"
            assert {f"system/web/static/{asset.name}" for asset in static.iterdir()} <= set(names)
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
        for name, script in (("status_check.py", STATUS_CHECK), ("web_check.py", WEB_CHECK)):
            check = folder / name
            check.write_text(script, encoding="utf-8")
            run(str(check))
    print(
        "PASS: pure-Python installed wheel, zero runtime dependencies, resource discovery, "
        "golden workflow, replay, the loopback read surface and its one command"
    )


if __name__ == "__main__":
    main()
