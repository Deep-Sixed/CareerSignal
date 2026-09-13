"""Installed command entrypoint plus runtime diagnostics that need no database."""

import sys
from importlib.metadata import PackageNotFoundError, version

from data.store import sqlite_report
from system.cli import main as application_main


def _package_version() -> str:
    try:
        return version("careersignal")
    except PackageNotFoundError:
        return "0.1.0"


def version_report() -> str:
    info = sqlite_report()
    fixed = "YES" if info["wal_reset_fix"] else "NO"
    lines = [
        f"CareerSignal {_package_version()}",
        f"Python {sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        f"SQLite {info['sqlite']}",
        f"Minimum SQLite: {info['minimum']}",
        f"WAL-reset fix: {fixed}",
    ]
    if not info["wal_reset_fix"]:
        lines.extend(
            [
                f"Nearest patched SQLite: {info['nearest_wal_reset_fix']}",
                "SQLite documents the WAL-reset race as rare; keep independent backups and "
                "serialize concurrent writers where practical.",
            ]
        )
    return "\n".join(lines)


def main():
    if sys.argv[1:] == ["version"]:
        print(version_report())
        return
    application_main()


if __name__ == "__main__":
    main()
