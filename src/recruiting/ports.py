"""External draft boundary. No sending capability is defined."""

from typing import Protocol


class DraftProvider(Protocol):
    def create(self, key: str, body: str, *, to: str = "", subject_line: str = "") -> str:
        """Return a receipt for this immutable draft intent.

        Addressing is passed rather than looked up, because a provider that could look it
        up would need to read the record it is writing about. Both values are untrusted
        recruiter text and it is the provider's job to refuse an unusable one.
        """

    def lookup(self, key: str) -> str | None:
        """Reconcile an uncertain attempt without repeating the write."""
