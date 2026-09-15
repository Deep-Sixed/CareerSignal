"""The local surface: a loopback HTTP projection of what is already stored, plus two commands.

Serving is all this package does. It holds no rule and owns no state beyond one launch token
and the destination an approval declares. It reaches the repository's read projections, and
exactly two of its writes -- `record_status` and `decide` -- each of which judges its own
precondition where the write happens rather than here. What this adds to the CLI is a shape
a browser can ask questions in and record two answers through, not a second place where the
answers are decided.
"""

from system.web.server import DEFAULT_PORT, LOOPBACK, Surface, serve

__all__ = ["DEFAULT_PORT", "LOOPBACK", "Surface", "serve"]
