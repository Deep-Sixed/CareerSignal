"""What an outward attempt did, decided from the durable record rather than the exception.

Two surfaces now run the same two actions -- the command line and the loopback web surface --
and an outward outcome is the one answer in this system where saying it two different ways
would be a safety defect rather than an inconsistency. REFUSED and UNCERTAIN are not degrees
of failure: one says nothing was created and the operator may correct and try again, the
other says something may exist and only reconciliation can say. A surface that classified
them independently could tell an operator to retry a draft that might already be in their
mailbox.

So the classification lives here, once, and each surface supplies only the words that
genuinely differ: where to go next is a sentence about that surface, not about the record.

The recorded intent decides, not the exception type. Past the claim the outward action has
already written what it knows -- an attempt whose result never came back is uncertain, not
failed -- so reading that back is the only honest answer. Before the claim nothing was
reserved and the refusal is certain.
"""

from recruiting.ports import DraftRefused, ProviderRejected

ACCEPTED, REFUSED, PROVIDER_REJECTED, UNCERTAIN = (
    "accepted",
    "refused",
    "provider_rejected",
    "uncertain",
)

# The two sentences a surface has to phrase for itself, because they name what the operator
# should do *here*. Everything else in the record is a fact about storage and is identical
# wherever it is read. `{review}` is substituted; nothing else is.
RECONCILE, IN_PROGRESS = "reconcile", "in_progress"


def attempt(repository, command: str, review: str, run, guidance: dict) -> dict:
    """Run one outward action and describe what the durable record now says.

    `run` is the caller's own invocation of the service, so the call to `draft()` or
    `reconcile()` is written where the surface using that authority lives. That is
    deliberate: the capability guard reads each package's syntax tree, and a surface that
    reached the outward workflow through a helper defined elsewhere would acquire it
    without the manifest ever noticing.

    `attempting` and `uncertain` are both unsettled, and they are not the same answer.
    UNCERTAIN says a provider was contacted and the result is unknown, which is exactly what
    makes reconciliation the next move. An `attempting` row says only that the write was
    reserved; whether anything was contacted is not recorded, so reporting it as uncertain
    claims more than the record holds.
    """
    common = {"command": command, "review": review}
    try:
        receipt = run()
    except DraftRefused as exc:
        # Read rather than assumed: a refusal reached before any intent exists (the usual
        # case for `draft`) truly has none to report, but an identity check that fails while
        # reconciling an already-unsettled intent leaves that intent exactly as it was.
        # Reporting it as None either time would say less than the record holds, or -- if
        # this were a fault instead -- more than it does.
        state = repository.intent(review)
        return {
            **common,
            "outcome": REFUSED,
            "state": state[0] if state else None,
            "receipt": state[1] if state else None,
            "message": str(exc),
            "next": "The existing draft intent is unaffected; correct the credential and try again."
            if state
            else "Nothing was created and nothing was sent; the approval still stands.",
        }
    except ProviderRejected as exc:
        # Proven, not merely unknown: the provider was contacted and its own response is
        # evidence nothing was created. The outward action has already released the durable
        # intent this attempt reserved and recorded the rejection, so there is nothing left
        # pending here -- the approval is untouched, and retrying once the cause is fixed is
        # exactly as safe as after an ordinary refusal, just not the same fact.
        return {
            **common,
            "outcome": PROVIDER_REJECTED,
            "state": None,
            "receipt": None,
            "message": str(exc),
            "next": "Nothing was created; correct the cause and run draft again.",
        }
    except (ValueError, KeyError) as exc:
        # Raised before any intent is reserved: missing approval, a changed review, a changed
        # address, an ended opportunity, or nothing to reconcile.
        return {**common, "outcome": REFUSED, "state": None, "receipt": None, "message": str(exc)}
    except (RuntimeError, OSError) as exc:
        state = repository.intent(review)
        if not state:
            # Nothing was reserved, so this is not an uncertain external result and must not
            # be reported as one. It is a fault, and a fault is not a state.
            raise
        return {
            **common,
            "outcome": UNCERTAIN,
            "state": state[0],
            "receipt": state[1],
            "message": f"the provider was contacted and the outcome is unknown: {exc}",
            "next": guidance[RECONCILE].format(review=review),
        }
    state = repository.intent(review)
    if receipt:
        return {
            **common,
            "outcome": ACCEPTED,
            "state": state[0] if state else None,
            "receipt": receipt,
            "message": f"draft confirmed; receipt {receipt}",
        }
    if command == "draft" and state and state[0] == "attempting":
        # This invocation reserved nothing, contacted nothing and wrote nothing: a claim
        # already stood, so it stopped. That is a refusal. Only `draft` reads this way --
        # `reconcile` does contact the provider to look, so its answer is about what the
        # lookup found, and refusing it here would close the one route out of this state.
        return {
            **common,
            "outcome": REFUSED,
            "state": state[0],
            "receipt": None,
            "message": "a draft attempt is already in progress; it is never retried automatically",
            "next": guidance[IN_PROGRESS].format(review=review),
        }
    return {
        **common,
        "outcome": UNCERTAIN,
        "state": state[0] if state else None,
        "receipt": None,
        "message": "a draft was attempted and its outcome is unknown; it is never retried "
        "automatically",
        "next": guidance[RECONCILE].format(review=review),
    }
