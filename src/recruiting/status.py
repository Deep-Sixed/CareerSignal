"""The status vocabulary an operator may record against an opportunity."""

# Written in the order a search usually runs, but the order carries no rule. Real searches
# jump: a referral can start at interviewing, an application can be withdrawn from any
# point, and a rejection can be revisited. Only the vocabulary is validated, so a status
# this project has no business rule about cannot be silently invented either.
STATUSES = (
    "new",
    "reviewing",
    "interested",
    "applied",
    "interviewing",
    "offer",
    "rejected",
    "withdrawn",
    "closed",
)
# Every opportunity starts here, recorded at intake rather than assumed by its absence.
INITIAL = "new"
# Statuses that describe a search still in progress. Reopening a rejected opportunity means
# recording one of these again; there is no separate "reopened" status, because the history
# already shows what it was reopened from.
ACTIVE = ("reviewing", "interested", "applied", "interviewing", "offer")
# Statuses that end the operator's interest in an opportunity. Recording one of these does
# not close the record -- history is append-only and an active status can follow -- but it
# does withdraw authority to act outward on the opportunity's behalf while it stands.
# "new" is deliberately in neither set: nothing has been decided about it yet.
TERMINAL = ("rejected", "withdrawn", "closed")


def validate(status) -> str:
    """Normalize a supplied status, or refuse it. Nothing outside the vocabulary is stored."""
    if not isinstance(status, str):
        raise ValueError("Status must be text")
    normalized = " ".join(status.split()).casefold()
    if normalized not in STATUSES:
        raise ValueError("Unknown status; expected one of " + ", ".join(STATUSES))
    return normalized


class StatusConflict(ValueError):
    """The opportunity moved between the read the operator acted on and the write.

    Raised instead of appending, so a decision taken against `interested` cannot land on
    top of a `withdrawn` recorded in the meantime and quietly become the current status.
    Nothing is stored: the ledger is append-only and this is not an event, it is the
    refusal to write one. The operator reads the current state again and decides again.

    A ValueError so that any caller already refusing on a bad status also refuses here
    rather than continuing on a write that did not happen; callers that can tell the
    operator more catch this first and report what was found.
    """

    def __init__(self, expected, observed, status):
        super().__init__(
            f"Opportunity status changed since it was read: expected event {expected}, "
            f"found event {observed}" + (f" ({status})" if status else " (no status history)")
        )
        self.expected, self.observed, self.status = expected, observed, status
