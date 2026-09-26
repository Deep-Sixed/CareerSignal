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
#
# A raw string, because the synthetic message below carries CRLF escapes that belong to
# the generated script rather than to this one: without it they would be folded into real
# line breaks here and arrive there as an unterminated literal.
WEB_CHECK = r"""
import http.client
import json
import os
import threading
from importlib import resources

from data.repository import Repository
from communications.controlled import ControlledDrafts
from communications.message import MAX_MESSAGE_BYTES
from recruiting.models import Profile
from system.intake import IntakeActions
from system.web.server import NAMESPACE_HEADER, RFC822, TOKEN_HEADER, Surface
from system.workflow import OutwardActions

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

surface = Surface(
    Repository("extraction.db"),
    port=0,
    provider="gmail",
    provider_namespace="gmail:operator@example.com",
)
threading.Thread(target=surface.serve_forever, daemon=True).start()


def ask(path, method="GET", token=True, body=None, origin=True, media="application/json",
        headers=(), length=None, transmit=True):
    '''One request, spoken to the surface rather than handed to urllib.

    `urlopen` cannot declare a body and withhold it, which several checks below need. This
    surface settles a refusal on the envelope -- provenance, media type, declared length,
    declared source -- before a byte of the body is read, so bytes actually sent at one of
    those addresses stay unread in the receive buffer. Closing on unread bytes is a reset
    rather than a clean shutdown on Windows, and whether the refusal or the reset reaches
    the client first is a race no check should be made to win.

    So `transmit=False` sends the headers and none of the body, and every call below that
    is refused before the read uses it. `length` declares something other than the truth,
    which is the only way to reach the size ceiling without writing at it. `Client.send` in
    src/system/tests/test_web_surface.py holds the same two controls for the same reason.

    Withholding a body an address *does* read would hang until the timeout instead, so this
    is for refusals settled before the read and nothing else.
    '''
    connection = http.client.HTTPConnection(*surface.server_address, timeout=30)
    try:
        connection.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
        connection.putheader("Host", surface.authority)
        if token:
            connection.putheader(TOKEN_HEADER, surface.token)
        if body is not None:
            connection.putheader("Content-Type", media)
            connection.putheader("Content-Length", str(len(body) if length is None else length))
        for name, value in headers:
            connection.putheader(name, value)
        if origin:
            connection.putheader("Origin", surface.origin)
        connection.endheaders(body if transmit else None)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


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

# The status command, from the installed wheel: the vocabulary is served, a status appends,
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

# Declared and withheld: provenance is settled before the body is read, so these two
# never send one. The malformed body below is sent, and has to be read to be refused.
assert ask(address, method="POST", body=order, origin=False, transmit=False)[0] == 403
assert ask(address, method="POST", body=order, token=False, transmit=False)[0] == 401
assert ask(address, method="POST", body=b"{oops", origin=True)[0] == 400

history = json.loads(ask("/api/v1/opportunities/" + chosen)[1])["history"]
assert [row["status"] for row in history][-1] == "applied", history
assert sum(1 for row in history if row["status"] == "applied") == 1, history
assert history[-1]["actor"] == "operator", history

# The decision command, from the installed wheel. The destination is the launch's, never
# the caller's, and no compose credential is configured here -- approving names where a
# draft may go and has never needed the ability to reach it.
facts = json.loads(ask("/api/v1/session")[1])
assert facts["gmail_compose_token"] is False, facts
assert facts["decision_target"] == {
    "provider": "gmail",
    "provider_namespace": "gmail:operator@example.com",
}, facts

detail = json.loads(ask("/api/v1/opportunities/" + chosen)[1])
review, packet = detail["review"], detail["bound"]["expected"]
assert sorted(packet) == [
    "addressing_digest",
    "content_digest",
    "draft_digest",
    "status_event_id",
], packet
verdict = "/api/v1/reviews/" + review + "/decision"

# An approval carrying the packet that was read is recorded against the launch destination.
status, body = ask(verdict, method="POST", body=json.dumps({"approved": True, "expected": packet}).encode())
assert status == 200, (status, body)
assert json.loads(body)["provider_namespace"] == "gmail:operator@example.com", body
action = json.loads(ask("/api/v1/opportunities/" + chosen)[1])["action"]
assert (action["decision"], action["binds"]) == ("approved", True), action
assert action["provider_namespace"] == "gmail:operator@example.com", action

# The same approval again, after the status moved under it: refused, and nothing written.
moved = json.dumps({"status": "interviewing", "expected_event_id": json.loads(
    ask("/api/v1/opportunities/" + chosen)[1])["status_event"]}).encode()
assert ask(address, method="POST", body=moved)[0] == 200
settled = ask("/api/v1/timeline")[1]
status, body = ask(verdict, method="POST", body=json.dumps({"approved": True, "expected": packet}).encode())
assert status == 409, (status, body)
assert json.loads(body)["error"] == "binding_conflict", body
assert ask("/api/v1/timeline")[1] == settled, "a refused decision still moved a ledger"

# A rejection needs no packet, and an approval may not go without one.
assert ask(verdict, method="POST", body=b'{"approved": true}')[0] == 400
assert ask(verdict, method="POST", body=b'{"approved": false, "expected": {}}')[0] == 400
assert ask(verdict, method="POST", body=b'{"approved": false, "actor": "somebody"}')[0] == 400
assert ask(
    verdict, method="POST", body=b'{"approved": false}', origin=False, transmit=False
)[0] == 403
assert ask(
    verdict, method="POST", body=b'{"approved": false}', token=False, transmit=False
)[0] == 401
assert ask("/api/v1/reviews/nope/decision", method="POST", body=b'{"approved": false}')[0] == 404
status, body = ask(verdict, method="POST", body=b'{"approved": false}')
assert status == 200, (status, body)
assert json.loads(ask("/api/v1/opportunities/" + chosen)[1])["action"]["decision"] == "rejected"

# The outward commands, from the installed wheel. This launch declares a Gmail destination
# and holds no compose credential, so it may record decisions and may not act on them: both
# addresses answer honestly, nothing is created, and the provenance guards hold exactly as
# they do for every other command.
assert facts["outward"] is False, facts
for command in ("draft", "reconcile"):
    outward = "/api/v1/reviews/" + review + "/" + command
    status, body = ask(outward, method="POST", body=b"{}")
    assert status == 409, (command, status, body)
    assert json.loads(body)["outcome"] == "refused", body
    # The address carries nothing. A field is refused rather than ignored.
    assert ask(outward, method="POST", body=b'{"actor": "somebody"}')[0] == 400, command
    assert ask(
        outward, method="POST", body=b"{}", origin=False, transmit=False
    )[0] == 403, command
    assert ask(
        outward, method="POST", body=b"{}", token=False, transmit=False
    )[0] == 401, command
    # Reading a command address is 404 rather than 405: the read router simply has no
    # such route, and these two answer POST only.
    assert ask(outward, method="GET")[0] == 404, command
assert ask("/api/v1/reviews/nope/draft", method="POST", body=b"{}")[0] == 404

surface.shutdown()

# And the same two commands on a launch that can reach a provider, so the accepted path is
# exercised from the wheel rather than only its refusal. The service is built here, outside
# system.web, exactly as the command line builds it.
store = Repository("extraction.db")
surface = Surface(
    store,
    port=0,
    provider="controlled",
    provider_namespace="controlled",
    actions=OutwardActions(store, ControlledDrafts()),
)
threading.Thread(target=surface.serve_forever, daemon=True).start()
assert json.loads(ask("/api/v1/session")[1])["outward"] is True

# The same opportunity the decision block finished with. The other one in this fixture is
# below threshold and cannot be approved at all, which is itself the engine refusing
# correctly -- it is simply not the review this check is about.
detail = json.loads(ask("/api/v1/opportunities/" + chosen)[1])
approval = json.dumps({"approved": True, "expected": detail["bound"]["expected"]}).encode()
verdict = "/api/v1/reviews/" + detail["review"] + "/decision"
status, body = ask(verdict, method="POST", body=approval)
assert status == 200, (status, body)

drafting = "/api/v1/reviews/" + detail["review"] + "/draft"
status, body = ask(drafting, method="POST", body=b"{}")
assert status == 200, (status, body)
created = json.loads(body)
assert created["outcome"] == "accepted", created
assert created["receipt"].startswith("controlled-"), created

# Asking again returns the receipt that already exists rather than making a second draft.
status, body = ask(drafting, method="POST", body=b"{}")
assert status == 200, (status, body)
assert json.loads(body)["receipt"] == created["receipt"], body
assert json.loads(ask("/api/v1/opportunities/" + chosen)[1])["action"]["draft"] == "confirmed"

surface.shutdown()

# Intake, from the installed wheel. A launch with no evaluation profile takes nothing in;
# one with a profile takes a local message in with no Gmail credential of any kind in the
# environment, which is the independence the two grants are supposed to have.
WHEEL_MESSAGE = (
    b"From: recruiter@example.com\r\nTo: operator@example.com\r\n"
    b"Subject: A wheel-synthetic role\r\nMessage-ID: <wheel-intake@example.com>\r\n"
    b'MIME-Version: 1.0\r\nContent-Type: text/plain; charset="utf-8"\r\n\r\n'
    b"Title: Wheel Engineer\r\nCompany: Example Company\r\nLocation: remote\r\n"
    b"Skills: python\r\nURL: https://jobs.example.com/wheel\r\n"
)
DECLARED = [(NAMESPACE_HEADER, "wheel:intake")]

store = Repository("intake.db")
surface = Surface(store, port=0)
threading.Thread(target=surface.serve_forever, daemon=True).start()
assert json.loads(ask("/api/v1/intake/sources")[1]) == {
    "eml": {"available": False},
    "gmail": {"available": False},
    "profile": None,
}, ask("/api/v1/intake/sources")[1]
for address, body, media, headers in (
    ("/api/v1/intake/eml", WHEEL_MESSAGE, RFC822, DECLARED),
    ("/api/v1/intake/gmail", b"{}", "application/json", ()),
):
    # Whether this launch takes anything in at all is settled before the body is read, so
    # both addresses declare theirs and send none.
    status, refused = ask(
        address, method="POST", body=body, media=media, headers=headers, transmit=False
    )
    assert status == 409, (address, status, refused)
    assert json.loads(refused)["error"] == "intake_unavailable", refused
assert json.loads(ask("/api/v1/communications")[1]) == [], "a refused intake still stored a message"
surface.shutdown()

# The same launch with a profile, and with no Gmail credential in the environment at all:
# local intake needs neither grant, and asks for neither.
for variable in ("CAREERSIGNAL_GMAIL_TOKEN", "CAREERSIGNAL_GMAIL_COMPOSE_TOKEN"):
    os.environ.pop(variable, None)

surface = Surface(store, port=0, inbound=IntakeActions(store, Profile(("python",))))
threading.Thread(target=surface.serve_forever, daemon=True).start()

found = json.loads(ask("/api/v1/intake/sources")[1])
assert found["eml"]["available"] is True, found
assert found["gmail"] == {"available": False}, found
assert found["profile"] == {"skills": ["python"], "locations": ["remote"]}, found
assert "token" not in json.dumps(found).casefold(), found

status, body = ask(
    "/api/v1/intake/eml", method="POST", body=WHEEL_MESSAGE, media=RFC822, headers=DECLARED
)
assert status == 200, (status, body)
taken = json.loads(body)
assert taken["source"] == "eml" and taken["namespace"] == "wheel:intake", taken
assert len(taken["reviews"]) == 1, taken
assert json.loads(ask("/api/v1/session")[1])["gmail_token"] is False

# The opportunity really is there, through the projections the browser reads.
listed = json.loads(ask("/api/v1/opportunities")[1])
assert [row["title"] for row in listed] == ["Wheel Engineer"], listed
assert [row["message"] for row in json.loads(ask("/api/v1/communications")[1])] == [
    taken["message"]
], taken

# Taking the same message in again resolves to what already exists rather than duplicating it.
status, again = ask(
    "/api/v1/intake/eml", method="POST", body=WHEEL_MESSAGE, media=RFC822, headers=DECLARED
)
assert status == 200, (status, again)
assert json.loads(again)["reviews"] == taken["reviews"], again
assert len(json.loads(ask("/api/v1/communications")[1])) == 1

# And the refusals: unusable material, a body over the ceiling, no declared source, and a
# format this address does not accept.
assert ask(
    "/api/v1/intake/eml", method="POST", body=b"Subject: nothing\r\n\r\n", media=RFC822,
    headers=DECLARED,
)[0] == 400
# The last three are settled on the envelope -- a missing source, a blank one, a media type
# this address does not accept -- so each declares its body and sends none. The unusable
# material above is the one refusal here that the surface has to read to reach.
assert ask(
    "/api/v1/intake/eml", method="POST", body=WHEEL_MESSAGE, media=RFC822, transmit=False
)[0] == 400
assert ask(
    "/api/v1/intake/eml", method="POST", body=WHEEL_MESSAGE, media=RFC822,
    headers=[(NAMESPACE_HEADER, "   ")], transmit=False,
)[0] == 400
assert ask(
    "/api/v1/intake/eml", method="POST", body=WHEEL_MESSAGE, media="application/json",
    headers=DECLARED, transmit=False,
)[0] == 415
# Declared rather than actually sent, for the same reason and one more: the surface refuses
# on the declaration, which is the property being checked, and writing two megabytes at a
# server that has already answered and closed would only prove which side notices first.
assert ask(
    "/api/v1/intake/eml",
    method="POST",
    body=WHEEL_MESSAGE,
    media=RFC822,
    headers=DECLARED,
    length=MAX_MESSAGE_BYTES + 1,
    transmit=False,
)[0] == 413
# Gmail without a read credential stays a launch-capability refusal, not a network attempt.
assert ask("/api/v1/intake/gmail", method="POST", body=b"{}")[0] == 409
# A request may not name a fact the launch owns. This launch has a profile, so the command is
# parsed -- and refused for naming a field no request may carry.
assert ask(
    "/api/v1/intake/gmail", method="POST", body=b'{"skills": ["rust"]}'
)[0] == 400, "a request named the evaluation profile and was not refused"
assert ask("/api/v1/intake/eml", method="GET")[0] == 404
# Nothing was added by any of those refusals.
assert len(json.loads(ask("/api/v1/communications")[1])) == 1

surface.shutdown()
print("read surface and every command it answers verified from the installed wheel")
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
        "golden workflow, replay, the loopback surface and the commands it answers"
    )


if __name__ == "__main__":
    main()
