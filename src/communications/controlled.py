"""Synthetic structured messages and a thread-safe, non-network draft provider."""

import json
from threading import Lock

from recruiting.models import fingerprint


def parse_message(body: str) -> list:
    """Decode the envelope. A malformed envelope is unreadable; a malformed job is not."""
    value = json.loads(body)
    if not isinstance(value, dict) or not isinstance(value.get("jobs"), list) or not value["jobs"]:
        raise ValueError("Expected a nonempty jobs array")
    return value["jobs"]


class ControlledDrafts:
    """An in-memory provider for local certification, never a mailbox connection."""

    def __init__(self):
        self._drafts: dict[str, tuple[str, str]] = {}
        self._lock = Lock()
        self.calls = 0

    def refusal(self, key: str, body: str, *, to: str = "", subject_line: str = "") -> str | None:
        """Nothing to refuse: this provider composes no headers and contacts nothing."""
        return None

    def create(self, key: str, body: str, *, to: str = "", subject_line: str = "") -> str:
        with self._lock:
            self.calls += 1
            # Addressing is part of the content: the same words to a different recipient is
            # a different draft, and replaying one as the other would be a silent misdelivery.
            content = [body, to, subject_line]
            if key in self._drafts and self._drafts[key][0] != content:
                raise ValueError("Idempotency key reused with changed content")
            receipt = "controlled-" + fingerprint([key, content])
            self._drafts[key] = (content, receipt)
            return receipt

    def lookup(self, key: str) -> str | None:
        with self._lock:
            record = self._drafts.get(key)
            return record[1] if record else None
