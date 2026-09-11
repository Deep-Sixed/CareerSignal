"""External draft boundary. No sending capability is defined."""

from typing import Protocol


class DraftRefused(RuntimeError):
    """No draft was written or created, and no durable draft intent was reserved.

    Distinct from a failure that may have happened after the provider was contacted. A
    refusal is certain: it is not an unknown external result, so it must not be recorded
    as one. The evidence that caused it is retained unchanged and the operator's approval
    survives, so a corrected source can be drafted later.

    This can still follow a read. Proving which destination a credential belongs to is a
    read-only request, made solely to verify identity before anything is claimed, and it is
    not a draft write. A refusal reached after one is exactly as certain as one reached
    without it.
    """


class DraftProvider(Protocol):
    # Which implementation owns the external action this provider takes, e.g. "controlled"
    # or "gmail". Fixed per implementation: it names what the provider is, not what mailbox
    # any one instance was configured for.
    provider: str
    # The destination this instance was declared for, e.g. "gmail:alice@example.com" --
    # the operator's stated mailbox, not yet verified. Comparing this against an approval
    # is free: it is two strings, and answering it commits the provider to nothing.
    namespace: str

    def refusal(self, key: str, body: str, *, to: str = "", subject_line: str = "") -> str | None:
        """Why this draft cannot be created, or None if it can. Makes no request.

        Exists so a refusal can be discovered before any durable intent is reserved. A
        provider must answer this without contacting anything.
        """

    def identity(self) -> str:
        """The destination this instance is authorized for, e.g. "gmail:alice@example.com".

        For a provider with nothing to verify this is a declared constant. For one backed
        by a real credential this proves what the credential is, so an approval bound to
        one destination cannot be discharged by a request for a different one -- however
        the operator's own --mailbox has been typed. The property is on the provider, not
        the port function alone, because callers compare a request's declared identity
        against an approval before ever asking a provider to verify anything.
        """

    def create(self, key: str, body: str, *, to: str = "", subject_line: str = "") -> str:
        """Return a receipt for this immutable draft intent.

        Addressing is passed rather than looked up, because a provider that could look it
        up would need to read the record it is writing about. Both values are untrusted
        recruiter text and it is the provider's job to refuse an unusable one.
        """

    def lookup(self, key: str) -> str | None:
        """Reconcile an uncertain attempt without repeating the write."""
