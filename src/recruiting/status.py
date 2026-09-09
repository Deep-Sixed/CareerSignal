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


def validate(status) -> str:
    """Normalize a supplied status, or refuse it. Nothing outside the vocabulary is stored."""
    if not isinstance(status, str):
        raise ValueError("Status must be text")
    normalized = " ".join(status.split()).casefold()
    if normalized not in STATUSES:
        raise ValueError("Unknown status; expected one of " + ", ".join(STATUSES))
    return normalized
