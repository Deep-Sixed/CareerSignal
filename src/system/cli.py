"""Explicit local CLI; no network operations or automatic approvals outside demo."""

import argparse
import json
import os

from communications.controlled import ControlledDrafts
from communications.gmail import TOKEN_VARIABLE, GmailCredentials, GmailReader
from communications.message import MAX_MESSAGE_BYTES, Message
from data.repository import Repository
from data.store import database_path, migrate, verify_contract
from recruiting.models import Profile
from system.demo import golden_workflow
from system.workflow import Workflow


def main():
    parser = argparse.ArgumentParser(description="CareerSignal local foundation")
    parser.add_argument("command", choices=("init", "verify", "demo", "ingest", "gmail-ingest"))
    parser.add_argument("--message", help="Local RFC email file, used only by ingest")
    parser.add_argument("--namespace", help="Stable mailbox/source identifier for ingest")
    parser.add_argument(
        "--skill", action="append", help="Configured skill; repeat for multiple skills"
    )
    parser.add_argument("--location", action="append", help="Allowed location; defaults to remote")
    parser.add_argument(
        "--db", help="Database path; relative paths resolve from the current directory"
    )
    parser.add_argument("--mailbox", help="Authorized Gmail address, used only by gmail-ingest")
    parser.add_argument("--query", help="Gmail search query, used only by gmail-ingest")
    parser.add_argument("--label", action="append", help="Gmail label id; repeat for multiple")
    parser.add_argument(
        "--limit", type=int, default=25, help="Maximum Gmail messages to read in one run"
    )
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
        workflow = Workflow(
            repository,
            ControlledDrafts(),
            Profile(tuple(args.skill), tuple(args.location or ["remote"])),
        )
        result = {
            "reviews": workflow.intake_message(message),
            "diagnostics": [
                {"item": row[0], "reason": row[2], "review": row[4]}
                for row in repository.extraction_evidence(message.key)
            ],
        }
    elif args.command == "gmail-ingest":
        # The token is read from the environment only, never from a flag: a command line is
        # visible in shell history and to other users of the machine.
        token = os.getenv(TOKEN_VARIABLE, "")
        if not token.strip():
            parser.error(f"gmail-ingest requires {TOKEN_VARIABLE} in the environment")
        if not args.mailbox or not args.skill:
            parser.error("gmail-ingest requires --mailbox and at least one --skill")
        reader = GmailReader(GmailCredentials(token, args.mailbox))
        repository = Repository(path)
        workflow = Workflow(
            repository,
            ControlledDrafts(),
            Profile(tuple(args.skill), tuple(args.location or ["remote"])),
        )
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
                    "reviews": workflow.intake_message(message),
                }
                for message in messages
            ],
        }
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
