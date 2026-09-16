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
DATABASE_ENGINES = {"sqlite3", "libsql", "turso", "pysqlite3"}
DATABASE_OWNER = "src/data/store.py"
EXCLUDED = {".git", ".venv", ".pytest_cache", ".ruff_cache", "__pycache__", "dist", "build", "var"}
ROOT_FILES = {"README.md", "AGENTS.md", "pyproject.toml", "uv.lock", ".gitignore"}
# RFC 2606 reserves these names, and the .example top-level domain, for documentation and
# synthetic fixtures. A subdomain of a reserved name is reserved with it, so a fixture may
# say alerts@jobs.example.com without that being a real address anyone can receive mail at.
# The attribution domain is listed because commit trailers legitimately carry it.
RESERVED_DOMAINS = ("example.com", "example.net", "example.org")
ATTRIBUTION_DOMAIN = "users.noreply.github.com"
# The first reviewed binaries in the tree: static PNG snapshots of the frozen UI design
# artboards, which exist so the design can be read from a clone without the Claude Design
# runtime. Deliberately narrow -- one exact directory, one suffix, no nesting -- because the
# rule being relaxed is "no unreviewed binaries", not "no binaries under docs".
RENDER_DIRECTORY = ("docs", "ui-design", "renders")
# The second, and the only capture of the running application: the README's front-page
# screenshot. Named exactly, as one file, rather than by directory.
#
# A directory allowance would have been shorter and is the wrong shape. These two exceptions
# are not the same claim: a render under RENDER_DIRECTORY says "this is the design that was
# accepted", while this says "this is what CareerSignal does". The second is a statement
# about the product, made on the repository's front page, and it was only made after a
# review established that the image first proposed there was a design mock -- one whose own
# wordmark read MOCK and whose copy asserted something `test_web_frontend` forbids the
# shipped browser from saying. A second product screenshot is a second such claim and needs
# its own review, which is exactly what naming one file forces and a directory would not.
PRODUCT_SCREENSHOT = ("docs", "screenshots", "dashboard.png")
# Both exceptions are for reviewed PNGs, so a file must actually be one. A name is not
# evidence: without this, any binary at all renamed to .png would inherit an allowance,
# which is precisely the thing the "no unreviewed binaries" rule exists to stop.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def synthetic_domain(domain: str) -> bool:
    """Whether an address domain is reserved for documentation rather than deliverable."""
    value = domain.strip().rstrip(".").casefold()
    if value == ATTRIBUTION_DOMAIN or value.endswith(".example"):
        return True
    return any(value == name or value.endswith("." + name) for name in RESERVED_DOMAINS)


def _png(path) -> bool:
    """Whether these bytes actually begin a PNG.

    Shared by both allowances below, so neither can be satisfied by a name alone and the
    two cannot drift into checking different things.
    """
    try:
        with open(path, "rb") as stream:
            return stream.read(len(PNG_SIGNATURE)) == PNG_SIGNATURE
    except OSError:
        return False


def reviewed_render(rel, path) -> bool:
    """Whether this is one of the reviewed design renders, and nothing merely like one.

    Both halves are required: the path must be exactly where a reviewed render lives, and
    the bytes must actually begin a PNG. Either alone would admit something the argument
    for this exception never covered.
    """
    parts = tuple(rel.parts)
    if (
        len(parts) != len(RENDER_DIRECTORY) + 1
        or parts[: len(RENDER_DIRECTORY)] != RENDER_DIRECTORY
        or rel.suffix.lower() != ".png"
    ):
        return False
    return _png(path)


def reviewed_screenshot(rel, path) -> bool:
    """Whether this is the one reviewed capture of the running application.

    An exact path rather than a directory, and deliberately so: `docs/screenshots/` is not
    an allowance, `docs/screenshots/dashboard.png` is. A second file dropped beside it stops
    the gate and has to be argued for, which is the whole difference between an exception
    and a loophole.
    """
    return tuple(rel.parts) == PRODUCT_SCREENSHOT and _png(path)


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
    count = renders = screenshots = 0
    for path in files():
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
            # The reviewed renders and the one reviewed screenshot are the only binaries
            # that pass here. Everything else still stops the gate, including another binary
            # sitting in either directory.
            if reviewed_render(rel, path):
                renders += 1
            elif reviewed_screenshot(rel, path):
                screenshots += 1
            else:
                errors.append(f"Non-text artifact requires review: {name}")
            continue
        count += 1
        if any(p.search(text) or p.search(name) for p in patterns):
            errors.append(f"Private identity match: {name}")
        for email in emails.findall(text):
            if not synthetic_domain(email.rsplit("@", 1)[1]):
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
                if top in DATABASE_ENGINES and name != DATABASE_OWNER:
                    errors.append(
                        f"Database engine import outside {DATABASE_OWNER}: {name} -> {target}"
                    )
    if errors:
        raise SystemExit("\n".join(sorted(set(errors))))
    print(
        f"PASS: {count} publishable text files, {renders} reviewed renders and "
        f"{screenshots} reviewed screenshots; ownership, imports, artifacts, identity checks"
    )


if __name__ == "__main__":
    inspect()
