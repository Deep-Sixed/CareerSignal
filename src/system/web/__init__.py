"""The local surface: a loopback HTTP projection of what is already stored, plus its commands.

Serving is all this package does. It holds no rule and owns no state beyond one launch token
and the destination an approval declares. It reaches the repository's read projections and
the mutations declared in `docs/web-surface.md`, each of which judges its own precondition
inside the write that performs it rather than here. What this adds to the CLI is a shape a
browser can ask questions in and record those answers through, not a second place where the
answers are decided.

The declaration in that document is the canonical one and is compared against this package's
own syntax tree by a test, so this docstring does not enumerate the commands: a count kept in
several files is a count that goes stale in several files.
"""

from system.web.server import DEFAULT_PORT, LOOPBACK, Surface, serve

__all__ = ["DEFAULT_PORT", "LOOPBACK", "Surface", "serve"]
