"""Render query results for a terminal.

Presentation only: nothing here opens a database, decides anything, or changes state. No
truncation, so a title is never silently cut.

This is the first surface that prints externally sourced text to a terminal. A job title
comes from a recruiter's message, and `recruiting.models.clean` collapses whitespace but
leaves control characters intact, so a title can carry an escape sequence that retitles the
window or clears the screen. Terminal-bound text is therefore escaped here, at the
presentation boundary, and nowhere else: the stored evidence keeps exactly what arrived, so
the record of what was actually sent is never quietly rewritten. The JSON form needs no such
treatment because json.dumps already escapes control characters.
"""

import re

# The deliberately minimal operator list. Everything else a row carries -- location, URL,
# score components, last status change -- is in the JSON form and in the detail view.
COLUMNS = (
    ("STATUS", "status"),
    ("COMPANY", "company"),
    ("TITLE", "title"),
    ("COVERAGE", "coverage"),
    ("ELIGIBLE", "eligible"),
)
MISSING = "-"
# C0, DEL and C1. Everything outside these ranges, including ordinary Unicode, is printed
# unchanged: this makes text safe, it does not make it ASCII.
CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def safe(value) -> str:
    """One line of terminal text that cannot execute."""
    return CONTROL.sub(lambda found: f"\\x{ord(found.group()):02x}", str(value))


def safe_lines(value) -> list:
    """Multi-line text as safe lines: a newline stays structure, everything else escapes.

    Split on LF alone, after folding CRLF into it. str.splitlines() also breaks on bare CR,
    VT, FF, FS, GS, RS, NEL and the Unicode line separators, which would let those vanish
    into structure instead of being escaped -- the opposite of what this module promises.
    A trailing newline therefore yields a trailing empty line, which is what it is.
    """
    return [safe(line) for line in str(value).replace("\r\n", "\n").split("\n")]


def coverage(row) -> str:
    """Say why a number is absent rather than leaving a blank the operator must guess at."""
    if row["review"] is None:
        return MISSING
    if not row["actionable"]:
        return "pre-coverage"
    if not row["scored"]:
        return "not scored"
    return f"{row['coverage']}%"


def cell(row, field) -> str:
    if field == "coverage":
        return coverage(row)
    value = row[field]
    if value is None:
        return MISSING
    if isinstance(value, bool):
        return "yes" if value else "no"
    return safe(value)


# How an approval and a draft attempt read to an operator. Refused and uncertain are
# deliberately different sentences: one means nothing left this machine, the other means
# something may have, and the operator's next move differs.
APPROVAL = {"approved": "approved by", "rejected": "rejected by", None: "not yet decided"}
# Said once, so the three remediations differ only in the part that is actually different.
STALE = "stale: does not bind the material below"
DRAFTS = {
    "none": "not attempted",
    "refused": "refused; nothing was created",
    "attempting": "attempting; no outcome recorded yet",
    "uncertain": "uncertain; reconciliation required",
    "confirmed": "created",
}


def approval(action) -> str:
    """Who decided, or that nobody has, and whether that decision binds what is shown.

    An approval recorded against one recipient stays on record after a later message
    moves the target. Printing it plainly beside the new material would tell the operator
    they have authorized something they have not: the words are true about the past and
    misleading about the present. Only an approval is qualified this way -- a rejection
    authorizes nothing, so there is nothing for it to have stopped binding.

    The actor is operator-supplied text.
    """
    if action["decision"] is None:
        return APPROVAL[None]
    decided = f"{APPROVAL[action['decision']]} {safe(action['actor'])}"
    if action["decision"] == "approved" and not action["binds"]:
        # Say what can be done, not what would be refused. Once a draft has been
        # attempted, decide() locks the decision, so telling the operator to reapprove
        # would send them at a wall -- and the three ways out differ. Whether an intent
        # exists comes from the row, as `attempted`; which one it is, is the state name
        # this module already renders directly below.
        if not action["attempted"]:
            return f"{decided} ({STALE}; reapprove)"
        if action["draft"] == "confirmed":
            return f"{decided} ({STALE}; the earlier draft stands)"
        return f"{decided} ({STALE}; reconcile the attempt, do not retry)"
    return decided


def attempt(action) -> str:
    """The draft state, and the receipt when there is one to quote.

    A receipt comes from the provider, so it is escaped like any other outside text.
    """
    state = action["draft"]
    described = DRAFTS.get(state, safe(state))
    if state == "confirmed" and action["receipt"]:
        return f"{described}; receipt {safe(action['receipt'])}"
    return described


def outcome(record) -> str:
    """One command's result: what happened, and what the operator does about it.

    The whole line is escaped rather than trusted, because a refusal reason can quote a
    status, an actor or a provider receipt, and none of those originate here.
    """
    lines = [f"{record['outcome'].upper():<9} {safe(record['message'])}"]
    if record.get("next"):
        lines.append(f"          {safe(record['next'])}")
    return "\n".join(lines)


def table(rows) -> str:
    if not rows:
        return "No opportunities match."
    printed = [[cell(row, field) for _, field in COLUMNS] for row in rows]
    widths = [
        max(len(header), *(len(line[index]) for line in printed))
        for index, (header, _) in enumerate(COLUMNS)
    ]

    def line(values):
        # The last column is not padded, so no line carries trailing whitespace.
        return "  ".join(
            value.ljust(widths[index]) if index < len(widths) - 1 else value
            for index, value in enumerate(values)
        )

    return "\n".join([line([header for header, _ in COLUMNS])] + [line(v) for v in printed])


def detail(record) -> str:
    """One opportunity: what it is, what the current review said, and how it got here."""
    lines = [
        f"{safe(record['company'])} - {safe(record['title'])}",
        f"  id         {safe(record['id'])}",
        f"  url        {safe(record['url'])}",
        f"  location   {safe(record['location']) if record['location'] else MISSING}",
        f"  status     {cell(record, 'status')} (event {record['status_event']})",
        f"  coverage   {coverage(record)}",
        f"  eligible   {cell(record, 'eligible')}",
        f"  advances   {cell(record, 'advances')}",
        f"  approval   {approval(record['action'])}",
        f"  provider   {cell(record['action'], 'provider')}",
        f"  namespace  {cell(record['action'], 'provider_namespace')}",
        f"  draft      {attempt(record['action'])}",
    ]
    # Both are keyed on the opportunity's current review, so they are present together
    # or not at all; one condition, rather than two that could disagree.
    packet, bound = record["packet"], record["bound"]
    if bound:
        # What an approval would bind, shown before the operator gives one. The recipient
        # and subject are recruiter-supplied and reach a terminal for the first time here.
        lines.extend(
            [
                f"  review     {safe(bound['review'])}",
                f"  source     {safe(bound['source']) if bound['source'] else MISSING}",
                f"  recipient  {safe(bound['to']) if bound['to'] else MISSING}",
                f"  subject    {safe(bound['subject']) if bound['subject'] else MISSING}",
                "  reasons",
            ]
        )
        lines.extend(f"    {safe(reason)}" for reason in packet["reasons"])
        # Labelled for what it is. The line above says whether a draft was attempted;
        # this is the wording an approval binds to, which is a different thing. Printed
        # from the bound material rather than the payload's copy of it, so that what is
        # read here is what the approval's digest is taken over.
        lines.append("  wording")
        lines.extend(f"    {part}" for part in safe_lines(bound["wording"]))
    else:
        lines.append("  reasons    no current review")
    lines.append("  history")
    for event in record["history"]:
        lines.append(
            f"    {event['event']}  {safe(event['created_at'])}  {safe(event['status'])}  "
            f"{safe(event['actor'])}"
        )
        # An operator's reason may span lines. Each one is printed on its own row so the
        # newline stays structural and no single row carries anything executable.
        lines.extend(f"      {line}" for line in safe_lines(event["reason"]) if line)
    return "\n".join(lines)
