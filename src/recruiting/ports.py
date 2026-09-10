"""External draft boundary. No sending capability is defined."""

from typing import Protocol


class DraftRefused(RuntimeError):
    """The draft was not created and nothing left this machine.

    Distinct from a failure that may have happened after the provider was contacted. A
    refusal is certain: it is not an unknown external result, so it must not be recorded
    as one. The evidence that caused it is retained unchanged and the operator's approval
    survives, so a corrected source can be drafted later.
    """


class DraftProvider(Protocol):
    def refusal(self, key: str, body: str, *, to: str = "", subject_line: str = "") -> str | None:
        """Why this draft cannot be created, or None if it can. Makes no request.

        Exists so a refusal can be discovered before any durable intent is reserved. A
        provider must answer this without contacting anything.
        """

    def create(self, key: str, body: str, *, to: str = "", subject_line: str = "") -> str:
        """Return a receipt for this immutable draft intent.

        Addressing is passed rather than looked up, because a provider that could look it
        up would need to read the record it is writing about. Both values are untrusted
        recruiter text and it is the provider's job to refuse an unusable one.
        """

    def lookup(self, key: str) -> str | None:
        """Reconcile an uncertain attempt without repeating the write."""
