"""Explicit local CLI; no network operations or automatic approvals outside demo."""

import argparse
import json

from data.store import database_path, migrate, verify_contract
from system.demo import golden_workflow


def main():
    parser = argparse.ArgumentParser(description="CareerSignal local foundation")
    parser.add_argument("command", choices=("init", "verify", "demo"))
    parser.add_argument(
        "--db", help="Database path; relative paths resolve from the current directory"
    )
    args = parser.parse_args()
    path = database_path(args.db)
    if args.command == "init":
        result = {"applied": migrate(path)}
    elif args.command == "verify":
        verify_contract(path)
        result = {"contract": "PASS"}
    else:
        result = golden_workflow(path)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
