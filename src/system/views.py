"""Render query results for a terminal.

Presentation only: nothing here opens a database, decides anything, or changes state. Plain
ASCII and no truncation, so a title is never silently cut and the output is the same on
every console this project supports.
"""

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
    return str(value)


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
        f"{record['company']} - {record['title']}",
        f"  id         {record['id']}",
        f"  url        {record['url']}",
        f"  location   {record['location'] or MISSING}",
        f"  status     {cell(record, 'status')}",
        f"  coverage   {coverage(record)}",
        f"  eligible   {cell(record, 'eligible')}",
        f"  advances   {cell(record, 'advances')}",
    ]
    packet = record["packet"]
    if packet:
        lines.append("  reasons")
        lines.extend(f"    {reason}" for reason in packet["reasons"])
        lines.append("  draft")
        lines.extend(f"    {part}" for part in packet["draft"].splitlines() or [""])
    else:
        lines.append("  reasons    no current review")
    lines.append("  history")
    for event in record["history"]:
        note = f"  {event['reason']}" if event["reason"] else ""
        lines.append(f"    {event['created_at']}  {event['status']}  {event['actor']}{note}")
    return "\n".join(lines)
