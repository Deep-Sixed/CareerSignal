"""External draft boundary. No sending capability is defined."""

from typing import Protocol


class DraftProvider(Protocol):
    def create(self, key: str, body: str) -> str:
        """Return a receipt for this immutable draft intent."""

    def lookup(self, key: str) -> str | None:
        """Reconcile an uncertain attempt without repeating the write."""
