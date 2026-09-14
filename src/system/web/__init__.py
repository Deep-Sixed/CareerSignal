"""The local read surface: a loopback HTTP projection of what is already stored.

Serving is all this package does. It holds no rule, owns no state beyond one launch token,
and reaches no write path -- the repository's read projections are the whole of its
vocabulary. What it adds to the CLI is a shape a browser can ask questions in, not a second
place where the answers are decided.
"""

from system.web.server import DEFAULT_PORT, LOOPBACK, Surface, serve

__all__ = ["DEFAULT_PORT", "LOOPBACK", "Surface", "serve"]
