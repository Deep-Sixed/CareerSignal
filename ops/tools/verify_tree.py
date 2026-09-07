"""Inspect publishable files without depending on Git or reading private runtime data."""

import ast
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OWNERS = {"recruiting", "communications", "data", "system"}
ALLOWED_IMPORTS = {
    "recruiting": set(),
    "communications": {"recruiting"},
    "data": {"recruiting"},
    "system": OWNERS,
}
EXCLUDED = {".git", ".venv", ".pytest_cache", ".ruff_cache", "__pycache__", "dist", "build", "var"}
ROOT_FILES = {"README.md", "AGENTS.md", "pyproject.toml", "uv.lock", ".gitignore"}


def files():
    for directory, folders, names in os.walk(ROOT):
        folders[:] = [f for f in folders if f not in EXCLUDED and not f.startswith(".venv-")]
        for name in names:
            yield Path(directory) / name


def inspect():
    errors, seen = [], set()
    private = os.getenv("CAREERSIGNAL_PRIVATE_PATTERNS", "")
    patterns = [re.compile(p, re.I) for p in private.split(";") if p]
    emails = re.compile(r"[A-Z0-9_.+%-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
    count = 0
    for path in files():
        count += 1
        rel = path.relative_to(ROOT)
        name = rel.as_posix()
        if name.casefold() in seen:
            errors.append(f"Case collision: {name}")
        seen.add(name.casefold())
        if rel.parts[0] not in {"src", "ops", "docs", ".github"} and name not in ROOT_FILES:
            errors.append(f"Unowned root file: {name}")
        if rel.parts[0] == "src" and (len(rel.parts) < 3 or rel.parts[1] not in OWNERS):
            errors.append(f"Unowned source: {name}")
        if path.suffix.lower() in {
            ".pdf",
            ".doc",
            ".docx",
            ".zip",
            ".gz",
            ".db",
            ".sqlite",
            ".kdbx",
        }:
            errors.append(f"Private/binary artifact requires review: {name}")
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            errors.append(f"Non-text artifact requires review: {name}")
            continue
        if any(p.search(text) or p.search(name) for p in patterns):
            errors.append(f"Private identity match: {name}")
        for email in emails.findall(text):
            domain = email.rsplit("@", 1)[1].lower()
            if domain not in {
                "example.com",
                "example.org",
                "example.net",
                "users.noreply.github.com",
            }:
                errors.append(f"Non-example email requires review: {name}")
        if path.suffix != ".py" or rel.parts[0] != "src" or "tests" in rel.parts:
            continue
        owner = rel.parts[1]
        for node in ast.walk(ast.parse(text)):
            imports = []
            if isinstance(node, ast.Import):
                imports = [n.name for n in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                imports = [node.module or ""]
            for target in imports:
                top = target.split(".")[0]
                if top in OWNERS and top != owner and top not in ALLOWED_IMPORTS[owner]:
                    errors.append(f"Forbidden dependency: {name} -> {target}")
                if ".tests" in target:
                    errors.append(f"Runtime depends on tests: {name}")
    if errors:
        raise SystemExit("\n".join(sorted(set(errors))))
    print(f"PASS: {count} publishable text files; ownership, imports, artifacts, identity checks")


if __name__ == "__main__":
    inspect()
