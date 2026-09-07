"""Explicit local CLI; no network operations or automatic approvals outside demo."""

import argparse
import json

from communications.controlled import ControlledDrafts
from communications.message import MAX_MESSAGE_BYTES, Message
from data.repository import Repository
from data.store import database_path, migrate, verify_contract
from recruiting.models import Profile
from system.demo import golden_workflow
from system.workflow import Workflow


def main():
    parser = argparse.ArgumentParser(description="CareerSignal local foundation")
    parser.add_argument("command", choices=("init", "verify", "demo", "ingest"))
    parser.add_argument("--message", help="Local RFC email file, used only by ingest")
    parser.add_argument("--namespace", help="Stable mailbox/source identifier for ingest")
    parser.add_argument(
        "--skill", action="append", help="Configured skill; repeat for multiple skills"
    )
    parser.add_argument("--location", action="append", help="Allowed location; defaults to remote")
    parser.add_argument(
        "--db", help="Database path; relative paths resolve from the current directory"
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
