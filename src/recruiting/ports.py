"""External draft boundary. No sending capability is defined."""

from typing import Protocol


class DraftRefused(RuntimeError):
    """This invocation wrote no draft and reserved no new durable intent.

    Distinct from a failure that may have happened after the provider was contacted. A
    refusal is certain: it is not an unknown external result, so it must not be recorded
    as one.

    Stated per invocation rather than per review, because the two differ once an intent can
    already exist: raised from a fresh `draft()`, nothing was ever reserved, the evidence
    that caused the refusal is retained unchanged, and the operator's approval survives so a
    corrected source can be drafted later. Raised from `reconcile()` against an
    already-unsettled intent, that intent is untouched and remains exactly as authoritative
    as it was -- this refusal is about what this attempt to resolve it did, not about
    whether one exists.

    This can still follow a read either way. Proving which destination a credential belongs
    to is a read-only request, made solely to verify identity before anything is claimed or
    looked up, and it is not a draft write. A refusal reached after one is exactly as
    certain as one reached without it.
    """


class ProviderRejected(RuntimeError):
    """The provider was contacted for this write, and its response proves none was created.

    A third outcome, distinct from both of the others `create()` can produce:

      DraftRefused      never contacted anything; certain by construction.
      ProviderRejected  contacted, and the response itself proves no draft exists.
      (anything else)   contacted, and the outcome is genuinely unknown -- uncertain.

    That middle case only exists where a provider's own documented contract guarantees a
    response proves non-creation: an authentication, authorization or request-validation
    rejection that its API is specified to return before any write is attempted, never
    merely because *a* response came back with a failing status. A provider that cannot
    make that case must leave the failure as an ordinary exception and let it fall to the
    uncertain path -- fewer proven rejections is always the safe direction to be wrong in,
    for exactly the reason DraftRefused's own certainty matters.

    Nothing here retries automatically. The approval this draft was claimed against is
    unaffected and untouched; what changes is that the durable intent this attempt
    reserved is released, so an explicit, corrected `draft` invocation can claim again
    without editing or discarding history -- the rejection itself is what the audit
    record durably keeps.
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

        May raise ProviderRejected instead of an ordinary exception, but only when the
        response itself proves nothing was created. Everything else -- a timeout, a
        connection reset, a malformed success body, an error a provider cannot attribute
        to a pre-write check -- is left as an ordinary exception, and the caller treats
        that as an unknown outcome rather than a proven one.
        """

    def lookup(self, key: str) -> str | None:
        """Reconcile an uncertain attempt without repeating the write."""
