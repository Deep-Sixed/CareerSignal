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
    """Multi-line text as safe lines: a newline stays structure, everything else escapes."""
    return [safe(line) for line in str(value).splitlines()] or [""]


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
        f"  status     {cell(record, 'status')}",
        f"  coverage   {coverage(record)}",
        f"  eligible   {cell(record, 'eligible')}",
        f"  advances   {cell(record, 'advances')}",
    ]
    packet = record["packet"]
    if packet:
        lines.append("  reasons")
        lines.extend(f"    {safe(reason)}" for reason in packet["reasons"])
        lines.append("  draft")
        lines.extend(f"    {part}" for part in safe_lines(packet["draft"]))
    else:
        lines.append("  reasons    no current review")
    lines.append("  history")
    for event in record["history"]:
        lines.append(
            f"    {safe(event['created_at'])}  {safe(event['status'])}  {safe(event['actor'])}"
        )
        # An operator's reason may span lines. Each one is printed on its own row so the
        # newline stays structural and no single row carries anything executable.
        lines.extend(f"      {line}" for line in safe_lines(event["reason"]) if line)
    return "\n".join(lines)
