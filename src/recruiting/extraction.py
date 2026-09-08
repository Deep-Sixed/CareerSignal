"""Deterministic extraction from explicitly labeled recruiting messages."""

import json
import re
from dataclasses import dataclass, replace

from recruiting.models import Opportunity

PARSER_VERSION = "labeled-v1"
# Diagnostics are kept for the operator, not for storage of whole payloads.
EXCERPT_LIMIT = 500
FIELD = re.compile(
    r"^\s*(title|role|job title|company|employer|location|skills|url|apply|job url)\s*:\s*(.*)$",
    re.I,
)
ALIASES = {
    "role": "title",
    "job title": "title",
    "employer": "company",
    "apply": "url",
    "job url": "url",
}


@dataclass(frozen=True)
class ExtractedItem:
    index: int
    excerpt: str
    opportunity: Opportunity | None
    reason: str


def extract(text: str) -> tuple[ExtractedItem, ...]:
    """Each Title/Role starts an independent item; missing values are never guessed."""
    blocks, fields, lines, conflicts = [], {}, [], set()

    def finish():
        if not fields:
            return
        reason, job = "", None
        missing = sorted({"title", "company", "url"} - {k for k, v in fields.items() if v.strip()})
        if conflicts:
            reason = "Conflicting fields: " + ", ".join(sorted(conflicts))
        elif missing:
            reason = "Missing required fields: " + ", ".join(missing)
        else:
            try:
                job = Opportunity.normalize(
                    {
                        **fields,
                        "skills": [
                            s.strip()
                            for s in re.split(r"[,;]", fields.get("skills", ""))
                            if s.strip()
                        ],
                    }
                )
            except (ValueError, TypeError) as exc:
                reason = str(exc)
        blocks.append(ExtractedItem(len(blocks), "\n".join(lines), job, reason))

    for line in text.splitlines():
        match = FIELD.match(line)
        if not match:
            if fields:
                lines.append(line)
            continue
        field = ALIASES.get(match[1].lower(), match[1].lower())
        value = match[2].strip()
        if field == "title" and "title" in fields:
            finish()
            fields, lines, conflicts = {}, [], set()
        if field in fields and fields[field] != value:
            conflicts.add(field)
        fields[field] = value
        lines.append(line)
    finish()
    if not blocks:
        return (ExtractedItem(0, text, None, "No supported labeled opportunities found"),)
    return reject_conflicts(tuple(blocks))


def reject_conflicts(items: tuple[ExtractedItem, ...]) -> tuple[ExtractedItem, ...]:
    """Two different versions of one job URL in one message are ambiguous; reject both."""
    by_key, ambiguous = {}, set()
    for item in items:
        if item.opportunity:
            key = item.opportunity.key
            if key in by_key and by_key[key] != item.opportunity:
                ambiguous.add(key)
            by_key[key] = item.opportunity
    return tuple(
        replace(item, opportunity=None, reason="Conflicting versions of the same job URL")
        if item.opportunity and item.opportunity.key in ambiguous
        else item
        for item in items
    )


def extract_records(records) -> tuple[ExtractedItem, ...]:
    """Normalize each supplied record independently: one bad entry never discards the rest."""
    items = []
    for index, record in enumerate(records):
        excerpt = json.dumps(record, sort_keys=True, default=str)[:EXCERPT_LIMIT]
        try:
            items.append(ExtractedItem(index, excerpt, Opportunity.normalize(record), ""))
        except (ValueError, TypeError) as exc:
            items.append(ExtractedItem(index, excerpt, None, str(exc)))
    return reject_conflicts(tuple(items))
