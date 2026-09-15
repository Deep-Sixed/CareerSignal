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
serving until it is stopped, so it has no single outcome to report. What it serves is reads,
one status append and one decision -- each bounded where it is implemented rather than here:
a status carries the event it was recorded against, and an approval carries the packet it
was read from. Creating a draft and reconciling one stay on the command line, because those
are the actions that reach a mailbox, and authorizing one is not the same as doing it.

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
from communications.message import MAX_MESSAGE_BYTES, Message
from data.repository import Repository
from data.store import database_path, migrate, verify_contract
from recruiting.models import Profile
from recruiting.status import StatusConflict
from system import outward
from system.demo import golden_workflow
from system.outward import ACCEPTED, PROVIDER_REJECTED, REFUSED, UNCERTAIN
from system.views import detail, outcome, table
from system.web import DEFAULT_PORT, serve
from system.workflow import Intake, OutwardActions

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
        "--skill", action="append", help="Configured skill; repeat for multiple skills"
    )
    parser.add_argument("--location", action="append", help="Allowed location; defaults to remote")
    parser.add_argument(
        "--db", help="Database path; relative paths resolve from the current directory"
    )
    parser.add_argument(
        "--mailbox",
        help="Authorized Gmail address. Used by gmail-ingest, and by approve/serve to "
        "declare where an approval says a draft may go.",
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
        with open(args.message, "rb") as stream:
            message = Message.from_bytes(
                stream.read(MAX_MESSAGE_BYTES + 1), namespace=args.namespace
            )
        repository = Repository(path)
        intake = Intake(repository, Profile(tuple(args.skill), tuple(args.location or ["remote"])))
        result = {
            "reviews": intake.intake_message(message),
            "diagnostics": [
                {"item": row[0], "reason": row[2], "review": row[4]}
                for row in repository.extraction_evidence(message.key)
            ],
        }
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
        # unverified or mismatched mailbox identity on their own; this call is the same
        # adapter-owned guarantee, invoked early so a mismatch is caught before Repository
        # or Intake are even constructed rather than merely before the first message.
        reader.verify_identity()
        repository = Repository(path)
        intake = Intake(repository, Profile(tuple(args.skill), tuple(args.location or ["remote"])))
        # Read the whole batch before writing anything: no write reservation may be held
        # across a network call, and nothing here creates or sends a draft.
        messages = reader.messages(
            query=args.query or "", label_ids=tuple(args.label or ()), limit=args.limit
        )
        result = {
            "mailbox": reader.namespace,
            "read": len(messages),
            "messages": [
                {
                    "external_id": message.external_id,
                    "message": message.key,
                    "reviews": intake.intake_message(message),
                }
                for message in messages
            ],
        }
    elif args.command == "serve":
        # Loopback-only by construction: the surface offers no way to bind another
        # interface, and it reads, records a status and records a decision -- nothing else.
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
        serve(
            repository,
            port=args.port,
            provider=provider,
            provider_namespace=namespace,
            actions=actions,
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
