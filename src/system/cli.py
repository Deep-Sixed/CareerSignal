"""Explicit local CLI; no network operations or automatic approvals outside demo.

Two kinds of command live here, and they report differently on purpose.

The operator commands -- opportunities, opportunity, status, approve, reject, draft,
reconcile -- are read by a person. They print text by default and structured output behind
`--json`, and the ones that act report an outcome:

    ACCEPTED          (exit 0)  the thing asked for happened
    REFUSED           (exit 1)  it did not happen, and this machine decided that; reevaluate
    PROVIDER_REJECTED (exit 1)  a provider was contacted and its response proves it did not
                                happen either; correct the cause and retry, same as REFUSED
    UNCERTAIN         (exit 3)  a provider was contacted and the outcome is unknown; reconcile

Refused and uncertain are never collapsed into one failure. They call for different moves:
a refusal means this invocation wrote no draft and reserved no new durable intent -- though
verifying a Gmail identity ahead of a refusal may have read the mailbox's profile, and a
draft intent from an earlier invocation may already exist and stays exactly as it was -- and
the operator can correct and try again, while an uncertain result means something may have
been created and only reconciliation can say.

Provider-rejected shares REFUSED's exit code and its safety -- nothing was created, and
retrying once the cause is fixed is safe, with no reconciliation needed -- but is reported
under its own name because it is not the same fact: a refusal means no draft-create request
was ever sent, while a rejection means one was sent and the provider's own response proved
it failed. A provider may only report this when its response is documented to mean the
write never happened; anything it cannot prove that way is left as ordinary UNCERTAIN.

Argparse keeps exit 2 for a command that was written wrongly -- a missing argument, an
unknown id, a filter that cannot mean anything. That is a different thing from the system
refusing, so it stays a different code.

The pipeline commands -- init, verify, demo, ingest, gmail-ingest -- are run by scripts and
still emit a JSON document. They report no outcome because they have no operator decision
in them.

`serve` is neither. It binds a loopback socket and blocks, printing one address and then
serving until it is stopped, so it has no single outcome to report. What it serves is reads
and the six commands -- each bounded where it is implemented rather than here: a status
carries the event it was recorded against, an approval carries the packet it was read from,
the two outward commands carry nothing because what they would say was settled by the
approval, and the two intake commands carry material rather than policy. The outward pair
reports the same four outcomes listed above, from the same code, so a draft attempted from a
browser and one attempted here cannot be described differently.

Three authorities are decided at that launch, here, and they are independent. Creating a
draft needs a compose credential; taking in a mailbox needs a read credential, which is a
different grant; recording a decision has never needed either, and taking in a local `.eml`
needs no credential at all. So a Gmail launch without a compose token serves decisions and
refuses to act on them rather than refusing to start, and a launch may read a mailbox while
being unable to write to it, or the reverse. Making the safe half of the workflow depend on
the unsafe half is the dependency that was deliberately removed, and each of these keeps it
removed.

Intake also needs an evaluation profile, which `--skill` and `--location` supply the same
way they always have. It is a launch fact for a reason no surface may weaken: skills and
accepted locations decide which opportunities advance, and the material being judged is
written by a recruiter. A launch with no `--skill` therefore has no intake authority, and
says so rather than inventing a profile to judge against.

This module composes and prints. It holds no rule of its own: every refusal below comes
from the layer that owns it, and nothing here decides whether an action is authorized.
"""

import argparse
import json
import os

from communications.controlled import ControlledDrafts
from communications.gmail import TOKEN_VARIABLE, GmailCredentials, GmailReader
from communications.gmail_draft import (
    COMPOSE_TOKEN_VARIABLE,
    GmailComposeCredentials,
    GmailDrafts,
    namespace_for,
)
from communications.message import MAX_MESSAGE_BYTES
from data.repository import Repository
from data.store import database_path, migrate, verify_contract
from recruiting.models import Profile
from recruiting.status import StatusConflict
from system import outward
from system.demo import golden_workflow
from system.intake import IntakeActions
from system.outward import ACCEPTED, PROVIDER_REJECTED, REFUSED, UNCERTAIN
from system.views import detail, outcome, table
from system.web import DEFAULT_PORT, serve
from system.workflow import OutwardActions

CODES = {ACCEPTED: 0, REFUSED: 1, PROVIDER_REJECTED: 1, UNCERTAIN: 3}
# Where the operator goes next, phrased for a terminal. The classification itself is shared
# with the web surface; only these two sentences are about the surface being used.
GUIDANCE = {
    outward.RECONCILE: "Run: careersignal reconcile {review}",
    outward.IN_PROGRESS: "Inspect the opportunity, or run: careersignal reconcile {review}",
}
# Written out rather than built from the command name: "reject" + "d" is not a word, and a
# message the operator reads should not be assembled by string arithmetic.
DECIDED = {"approve": "approved", "reject": "rejected"}


def _provider(args, parser):
    """Choose where a draft is created. The external one is never chosen implicitly."""
    if args.provider != "gmail":
        return ControlledDrafts()
    token = os.getenv(COMPOSE_TOKEN_VARIABLE, "")
    if not token.strip():
        parser.error(f"--provider gmail requires {COMPOSE_TOKEN_VARIABLE} in the environment")
    if not args.mailbox:
        parser.error("--provider gmail requires --mailbox")
    return GmailDrafts(GmailComposeCredentials(token, args.mailbox))


def _outward_possible(args) -> bool:
    """Whether this launch could reach a provider at all, asked without building one.

    The controlled provider is local and always available. Gmail needs a compose credential
    and a mailbox, and their absence is not an error here the way it is for `draft`: an
    operator serving the UI to record decisions has not asked to create anything, and
    stopping them would be refusing the safe action because the unsafe one is unavailable.
    """
    if args.provider != "gmail":
        return True
    return bool(os.getenv(COMPOSE_TOKEN_VARIABLE, "").strip()) and bool(args.mailbox)


def _outward_provider(args):
    """The provider for a launch that has one. Only ever called after _outward_possible."""
    if args.provider != "gmail":
        return ControlledDrafts()
    return GmailDrafts(GmailComposeCredentials(os.getenv(COMPOSE_TOKEN_VARIABLE, ""), args.mailbox))


def _intake_profile(args) -> Profile:
    """The evaluation profile this launch scores against, from the launch and nowhere else.

    `--skill` and `--location` are the same two options `ingest` and `gmail-ingest` have
    always taken, and they stay operator configuration for the same reason: skills and
    accepted locations decide which opportunities advance, and the material being judged is
    written by a recruiter. A profile a request could name is a profile a message could name.
    """
    return Profile(tuple(args.skill), tuple(args.location or ["remote"]))


def _gmail_reader(args):
    """The read-only Gmail credential this launch holds, or None if it holds none.

    Read authority and draft authority are separate grants, separately configured and
    separately absent: this reads CAREERSIGNAL_GMAIL_TOKEN, never the compose token, so a
    launch may legitimately be able to read a mailbox and not write to it, or the reverse.
    The token comes from the environment only -- a command line is visible in shell history
    and to other users of the machine.
    """
    token = os.getenv(TOKEN_VARIABLE, "")
    if not token.strip() or not args.mailbox:
        return None
    return GmailReader(GmailCredentials(token, args.mailbox))


def _intake_service(repository, args, reader=None):
    """The one intake authority, built here where the adapters already live.

    Constructed outside `system.web` for the same reason the outward service is: that package
    imports no credential class and no provider, so what it can reach is what it was handed.
    A launch with no `--skill` has no profile to judge anything against and therefore no
    intake authority at all, which is a capability this returns None for rather than a
    degraded mode to work around.
    """
    if not args.skill:
        return None
    return IntakeActions(repository, _intake_profile(args), reader or _gmail_reader(args))


def _declared_identity(args, parser):
    """The provider identity an approval declares, without a credential.

    Deliberately not built from _provider(): approving does not need a working credential,
    only the same declaration a later draft attempt will be judged against. Tying it to
    whether Gmail can be reached right now would make recording an approval depend on
    something the approval step has never needed.
    """
    if args.provider != "gmail":
        return "controlled", "controlled"
    if not args.mailbox:
        parser.error("--provider gmail requires --mailbox")
    return "gmail", namespace_for(args.mailbox)


def known_review(repository, identifier, parser):
    """Stop on a review id that names nothing, before any command acts on it.

    An id that does not exist is a mistyped command, not the system refusing: the
    documented contract puts an unknown id with the other argparse errors at exit 2, and
    the opportunity commands already do this. Without it a nonexistent review reports the
    same REFUSED as a real one whose approval went stale -- and `reconcile` reported "No
    intent to reconcile", which reads as though the review existed and had not been
    drafted. A review that exists but is stale stays a refusal; only nonexistence is
    decided here.
    """
    try:
        repository.review(identifier)
    except KeyError:
        parser.error(f"No review with id {identifier}")


def report(record, structured: bool):
    """Print one command's outcome and stop with the code that outcome means.

    The record goes to stdout in both forms: it is the answer, not a diagnostic. Only
    argparse writes to stderr, and only for a command that was written wrongly.

    The JSON form carries the reason exactly as the refusing layer wrote it; the text form
    is escaped for a terminal. Automation therefore sees the underlying value and a person
    sees something that cannot execute -- the same split as the query commands.
    """
    print(json.dumps(record, indent=2) if structured else outcome(record))
    code = CODES[record["outcome"]]
    if code:
        raise SystemExit(code)


def attempted(actions, repository, args) -> dict:
    """Run a draft or a reconcile and describe what the durable record now says.

    The call to the service is written here rather than passed in as a name, because that
    is what makes this surface's outward authority visible: the capability guard reads this
    package's syntax tree, and an outward action reached through a helper defined elsewhere
    would not appear in it. The classification of the result is `system.outward`'s, shared
    with the web surface, so the two cannot drift into describing the same record
    differently.
    """
    review, command = args.identifier, args.command
    run = (
        (lambda: actions.draft(review))
        if command == "draft"
        else (lambda: actions.reconcile(review))
    )
    return outward.attempt(repository, command, review, run, GUIDANCE)


def main():
    parser = argparse.ArgumentParser(description="CareerSignal local foundation")
    parser.add_argument(
        "command",
        choices=(
            "init",
            "verify",
            "demo",
            "ingest",
            "gmail-ingest",
            "opportunities",
            "opportunity",
            "status",
            "approve",
            "reject",
            "draft",
            "reconcile",
            "serve",
        ),
    )
    parser.add_argument(
        "identifier",
        nargs="?",
        help="Opportunity id for the opportunity and status commands; review id for "
        "approve, reject, draft and reconcile",
    )
    parser.add_argument("--actor", help="Who is recording the decision; required to approve")
    parser.add_argument(
        "--provider",
        choices=("controlled", "gmail"),
        default="controlled",
        help="Where a draft is created, and for serve the destination an approval declares. "
        "'controlled' stays on this machine. 'gmail' creates a real draft in the authorized "
        "mailbox for draft, and for serve only names that mailbox as what an approval would "
        "authorize -- serve writes nothing to Gmail and needs no compose token. Never the "
        "default either way.",
    )
    parser.add_argument("--message", help="Local RFC email file, used only by ingest")
    parser.add_argument("--namespace", help="Stable mailbox/source identifier for ingest")
    parser.add_argument(
        "--skill",
        action="append",
        help="Configured skill; repeat for multiple skills. Required by ingest and "
        "gmail-ingest, and for serve it is what gives that launch intake authority at all: "
        "without one there is no profile to score incoming material against, so the browser "
        "is offered no intake control. A request may never supply one.",
    )
    parser.add_argument(
        "--location",
        action="append",
        help="Allowed location; defaults to remote. Launch configuration exactly as --skill "
        "is, for ingest, gmail-ingest and serve alike; a request may never supply one.",
    )
    parser.add_argument(
        "--db", help="Database path; relative paths resolve from the current directory"
    )
    parser.add_argument(
        "--mailbox",
        help="Authorized Gmail address. Used by gmail-ingest, by approve/serve to "
        "declare where an approval says a draft may go, and by serve to name the mailbox "
        "Gmail intake would read -- which needs CAREERSIGNAL_GMAIL_TOKEN, the read grant, "
        "and never the compose one. Whatever it is set to, the mailbox is proven against "
        "the credential before a message is taken in.",
    )
    parser.add_argument("--query", help="Gmail search query, used only by gmail-ingest")
    parser.add_argument("--label", action="append", help="Gmail label id; repeat for multiple")
    parser.add_argument(
        "--limit", type=int, default=25, help="Maximum Gmail messages to read in one run"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help="Port for the local read surface; it always binds 127.0.0.1 and nothing else",
    )
    parser.add_argument("--status", help="Filter opportunities by a single status")
    parser.add_argument("--to", help="Status to record, used only by the status command")
    parser.add_argument(
        "--expect",
        type=int,
        help="The status event the decision was made against; the status command records "
        "only if that event is still the latest one",
    )
    parser.add_argument("--reason", default="", help="Operator note stored with a status event")
    parser.add_argument("--active", action="store_true", help="Only searches still in progress")
    parser.add_argument("--eligible", action="store_true", help="Only location-eligible reviews")
    parser.add_argument(
        "--ineligible", action="store_true", help="Only reviews that are not location eligible"
    )
    parser.add_argument("--min-coverage", type=int, help="Stated skill coverage at or above")
    parser.add_argument("--max-coverage", type=int, help="Stated skill coverage at or below")
    parser.add_argument("--json", action="store_true", help="Structured output instead of a table")
    args = parser.parse_args()
    path = database_path(args.db)
    if args.command == "ingest":
        if not args.message or not args.namespace or not args.skill:
            parser.error("ingest requires --message, --namespace, and at least one --skill")
        # One byte past the ceiling, so an oversized file is refused by Message rather than
        # read whole and then measured. The read stays here because choosing a local file is
        # this command's own affordance: the service takes bytes, and no surface that is not
        # a command line may name a path at all.
        with open(args.message, "rb") as stream:
            raw = stream.read(MAX_MESSAGE_BYTES + 1)
        repository = Repository(path)
        record = _intake_service(repository, args).ingest_eml(raw, args.namespace)
        # Exactly the two fields this command has always printed. The service reports more --
        # the namespace, the message key, the provider id -- and adding them here would change
        # an established contract for scripts that already read this output. The orchestration
        # is shared; what each surface says about it is not.
        result = {"reviews": record["reviews"], "diagnostics": record["diagnostics"]}
    elif args.command in {"opportunities", "opportunity"}:
        # Read-only. These commands never record a status, decide a review, create a draft
        # or contact a mailbox; they only report what is already stored.
        if args.eligible and args.ineligible:
            parser.error("--eligible and --ineligible cannot both be given")
        repository = Repository(path)
        if args.command == "opportunity":
            if not args.identifier:
                parser.error("opportunity requires an opportunity id")
            try:
                record = repository.opportunity(args.identifier)
            except KeyError:
                parser.error(f"No opportunity with id {args.identifier}")
            print(json.dumps(record, indent=2) if args.json else detail(record))
            return
        try:
            rows = repository.opportunities(
                status=args.status,
                active=args.active,
                eligible=True if args.eligible else False if args.ineligible else None,
                min_coverage=args.min_coverage,
                max_coverage=args.max_coverage,
            )
        except ValueError as exc:
            parser.error(str(exc))
        print(json.dumps(rows, indent=2) if args.json else table(rows))
        return
    elif args.command == "status":
        # The one operator-facing write to the status ledger. It is a compare-and-append:
        # the operator names the event they decided against, and if the opportunity has
        # moved since, nothing is written. A surface that has just printed a status is
        # exactly where a blind append would land a stale decision on top of a newer one.
        if not args.identifier:
            parser.error("status requires an opportunity id")
        if not args.to:
            parser.error("status requires --to")
        if args.expect is None:
            parser.error(
                "status requires --expect <event>; read the opportunity to see its current "
                "status event"
            )
        if not args.actor or not args.actor.strip():
            parser.error("status requires --actor")
        repository = Repository(path)
        try:
            recorded = repository.record_status(
                args.identifier,
                args.to,
                actor=args.actor,
                reason=args.reason,
                expected_event_id=args.expect,
            )
        except StatusConflict as exc:
            # Caught before ValueError, which it is a kind of: this refusal can say what
            # was found, and the operator needs that to decide again.
            record = {
                "command": "status",
                "opportunity": args.identifier,
                "outcome": REFUSED,
                "status": exc.status,
                "event": exc.observed,
                "expected_event": exc.expected,
                "message": str(exc),
                "next": "Read the opportunity again and record against its current state.",
            }
        except (ValueError, KeyError) as exc:
            parser.error(str(exc))
        else:
            record = {
                "command": "status",
                "opportunity": args.identifier,
                "outcome": ACCEPTED,
                "status": recorded["status"],
                "event": recorded["event"],
                "expected_event": args.expect,
                "message": f"{recorded['status']} recorded as event {recorded['event']}",
            }
        report(record, args.json)
        return
    elif args.command in {"approve", "reject"}:
        if not args.identifier:
            parser.error(f"{args.command} requires a review id")
        if not args.actor or not args.actor.strip():
            parser.error(f"{args.command} requires --actor")
        provider, namespace = _declared_identity(args, parser)
        repository = Repository(path)
        known_review(repository, args.identifier, parser)
        try:
            repository.decide(
                args.identifier,
                approved=args.command == "approve",
                actor=args.actor,
                provider=provider,
                provider_namespace=namespace,
            )
        except (ValueError, KeyError) as exc:
            # A refusal by the decision rules, not a mistyped command: the operator can
            # correct what it names -- record an active status, re-ingest, approve the
            # current review -- and ask again.
            record = {
                "command": args.command,
                "review": args.identifier,
                "outcome": REFUSED,
                "message": str(exc),
            }
        else:
            record = {
                "command": args.command,
                "review": args.identifier,
                "outcome": ACCEPTED,
                "message": f"review {args.identifier} "
                f"{DECIDED[args.command]} by {args.actor.strip()}",
            }
        report(record, args.json)
        return
    elif args.command in {"draft", "reconcile"}:
        if not args.identifier:
            parser.error(f"{args.command} requires a review id")
        repository = Repository(path)
        known_review(repository, args.identifier, parser)
        actions = OutwardActions(repository, _provider(args, parser))
        report(attempted(actions, repository, args), args.json)
        return
    elif args.command == "gmail-ingest":
        # The token is read from the environment only, never from a flag: a command line is
        # visible in shell history and to other users of the machine.
        token = os.getenv(TOKEN_VARIABLE, "")
        if not token.strip():
            parser.error(f"gmail-ingest requires {TOKEN_VARIABLE} in the environment")
        if not args.mailbox or not args.skill:
            parser.error("gmail-ingest requires --mailbox and at least one --skill")
        reader = GmailReader(GmailCredentials(token, args.mailbox))
        # reader.identifiers()/fetch()/messages() already refuse to run against an
        # unverified or mismatched mailbox identity on their own, and IntakeActions verifies
        # again before it reads; this call is the same adapter-owned guarantee, invoked early
        # so a mismatch is caught before Repository is constructed and its migrations run,
        # rather than merely before the first message. Verification is cached on the reader,
        # so asking here costs no second profile read.
        reader.verify_identity()
        repository = Repository(path)
        # The batch is read whole before the first local write, which is the service's
        # contract rather than this command's arrangement of calls.
        record = _intake_service(repository, args, reader).ingest_gmail(
            query=args.query or "", label_ids=tuple(args.label or ()), limit=args.limit
        )
        # The three fields this command has always printed, and per message the same three.
        # The service also reports extraction diagnostics; surfacing them here would widen an
        # established contract, which this change is not for.
        result = {
            "mailbox": record["mailbox"],
            "read": record["read"],
            "messages": [
                {key: message[key] for key in ("external_id", "message", "reviews")}
                for message in record["messages"]
            ],
        }
    elif args.command == "serve":
        # Loopback-only by construction: the surface offers no way to bind another
        # interface, and it reads and answers the four commands declared in
        # docs/web-surface.md -- nothing else.
        # It blocks here until the operator stops it, so it returns rather than falling
        # through to the JSON report the pipeline commands print.
        #
        # The approval destination is declared the same way `approve` declares it, through
        # _declared_identity, which needs no credential: approving names where a draft may
        # go, and naming a destination has never required the ability to reach it. Only the
        # two resulting strings cross into system.web.
        provider, namespace = _declared_identity(args, parser)
        repository = Repository(path)
        # Outward authority is separate from that declaration, and deliberately optional.
        # Creating a draft does need a credential that can reach the mailbox, so a launch
        # without one records decisions and refuses to act on them rather than failing to
        # start: making the safe half of the workflow depend on the unsafe half is the
        # dependency PR 7 removed, and requiring a compose token to open the Approvals screen
        # would put it back. The service is constructed here, where the adapters already live,
        # and handed over already built -- system.web imports no provider and no credential.
        actions = (
            OutwardActions(repository, _outward_provider(args)) if _outward_possible(args) else None
        )
        # Intake authority is separate again, and separately optional. It needs an evaluation
        # profile, which is what `--skill` supplies; Gmail intake needs a read credential on
        # top of that, which is a different grant from the compose one above. All three are
        # decided here and the already-built service is handed over, so system.web imports no
        # credential, constructs no reader, and cannot acquire either authority by asking.
        serve(
            repository,
            port=args.port,
            provider=provider,
            provider_namespace=namespace,
            actions=actions,
            inbound=_intake_service(repository, args),
        )
        return
    elif args.command == "init":
        result = {"applied": migrate(path)}
    elif args.command == "verify":
        verify_contract(path)
        result = {"contract": "PASS"}
    else:
        result = golden_workflow(path)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
