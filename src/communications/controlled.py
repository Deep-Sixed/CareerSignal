"""Synthetic structured messages and a thread-safe, non-network draft provider."""

import json
from threading import Lock

from recruiting.models import Opportunity, fingerprint


def parse_message(body: str) -> list[Opportunity]:
    value = json.loads(body)
    if not isinstance(value, dict) or not isinstance(value.get("jobs"), list) or not value["jobs"]:
        raise ValueError("Expected a nonempty jobs array")
    return [Opportunity.normalize(item) for item in value["jobs"]]


class ControlledDrafts:
    """An in-memory provider for local certification, never a mailbox connection."""

    def __init__(self):
        self._drafts: dict[str, tuple[str, str]] = {}
        self._lock = Lock()
        self.calls = 0

    def create(self, key: str, body: str) -> str:
        with self._lock:
            self.calls += 1
            if key in self._drafts and self._drafts[key][0] != body:
                raise ValueError("Idempotency key reused with changed content")
            receipt = "controlled-" + fingerprint([key, body])
            self._drafts[key] = (body, receipt)
            return receipt

    def lookup(self, key: str) -> str | None:
        with self._lock:
            record = self._drafts.get(key)
            return record[1] if record else None
