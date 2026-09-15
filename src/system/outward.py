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

from recruiting.ports import DraftRefused, DraftUncertain, ProviderRejected

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


def recorded(repository, review) -> tuple:
    """The durable intent as a (state, receipt) pair, with absence spelled the same way.

    Every branch below reads this rather than asserting what the record must hold. That is
    the module's rule made mechanical: an answer that contradicts the row it describes is the
    one failure this whole vocabulary exists to prevent, and the cheapest way to prevent it
    is never to write the row's contents from memory.
    """
    return repository.intent(review) or (None, None)


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
        # Certain by construction: this invocation sent no draft-create request. What it may
        # still have is an intent from an earlier one -- an identity check that fails while
        # reconciling leaves that intent exactly as it was -- so the record is read rather
        # than assumed, either way.
        state, held = recorded(repository, review)
        return {
            **common,
            "outcome": REFUSED,
            "state": state,
            "receipt": held,
            "message": str(exc),
            "next": "The existing draft intent is unaffected; correct the credential and try again."
            if state
            else "Nothing was created and nothing was sent; the approval still stands.",
        }
    except ProviderRejected as exc:
        # Proven, not merely unknown: the provider was contacted and its own response is
        # evidence nothing was created. The outward action has already released the durable
        # intent this attempt reserved, so reading the record back should find nothing -- and
        # it is read rather than hardcoded, because a claim that survived a rejection is
        # something the operator needs told, not something this layer should paper over.
        state, held = recorded(repository, review)
        return {
            **common,
            "outcome": PROVIDER_REJECTED,
            "state": state,
            "receipt": held,
            "message": str(exc),
            "next": "Nothing was created; correct the cause and draft again.",
        }
    except DraftUncertain as exc:
        # Declared by the boundary that knows, never inferred here. A request was sent, its
        # outcome cannot be established, and an unsettled intent already stands -- `uncertain`
        # normally, or `attempting` where it was the settling write itself that failed. Which
        # one is read back rather than assumed, and both queue as reconcile. This is the one
        # outcome that must never suggest drafting again.
        state, held = recorded(repository, review)
        return {
            **common,
            "outcome": UNCERTAIN,
            "state": state,
            "receipt": held,
            "message": f"the provider was contacted and the outcome is unknown: {exc}",
            "next": guidance[RECONCILE].format(review=review),
        }
    except (ValueError, KeyError) as exc:
        # Refused before any request was sent: missing approval, a changed review, a changed
        # address, an ended opportunity, or nothing to reconcile. Anything raised *after*
        # contact arrives as DraftUncertain above, whatever class the provider raised, which
        # is what keeps this branch from having to guess which side of the boundary it is on.
        state, held = recorded(repository, review)
        return {
            **common,
            "outcome": REFUSED,
            "state": state,
            "receipt": held,
            "message": str(exc),
        }
    except Exception as exc:
        # A fault: not an outcome this vocabulary has a name for. If nothing is reserved it
        # is reported as the fault it is, because a fault is not a state. But an unsettled
        # intent outranks that -- something may exist in the mailbox, and letting the
        # exception escape would leave the operator with a traceback where the record has an
        # answer. Erring toward "reconcile" is the safe direction to err in.
        state, held = recorded(repository, review)
        if not state:
            raise
        return {
            **common,
            "outcome": UNCERTAIN,
            "state": state,
            "receipt": held,
            "message": f"the attempt did not complete and the record is unsettled: {exc}",
            "next": guidance[RECONCILE].format(review=review),
        }
    state, held = recorded(repository, review)
    if receipt:
        return {
            **common,
            "outcome": ACCEPTED,
            "state": state,
            "receipt": receipt,
            "message": f"draft confirmed; receipt {receipt}",
        }
    if command == "draft" and state == "attempting":
        # This invocation reserved nothing, contacted nothing and wrote nothing: a claim
        # already stood, so it stopped. That is a refusal. Only `draft` reads this way --
        # `reconcile` does contact the provider to look, so its answer is about what the
        # lookup found, and refusing it here would close the one route out of this state.
        return {
            **common,
            "outcome": REFUSED,
            "state": state,
            "receipt": None,
            "message": "a draft attempt is already in progress; it is never retried automatically",
            "next": guidance[IN_PROGRESS].format(review=review),
        }
    return {
        **common,
        "outcome": UNCERTAIN,
        "state": state,
        "receipt": None,
        "message": "a draft was attempted and its outcome is unknown; it is never retried "
        "automatically",
        "next": guidance[RECONCILE].format(review=review),
    }
