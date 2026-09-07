"""Compose pure recruiting rules, real storage and controlled draft actions."""

from communications.controlled import parse_message
from data.repository import Repository
from recruiting.models import Profile, evaluate, fingerprint
from recruiting.ports import DraftProvider


class Workflow:
    def __init__(self, repository: Repository, provider: DraftProvider, profile: Profile):
        self.repository, self.provider, self.profile = repository, provider, profile

    def intake(self, message_id: str, body: str) -> list[str]:
        jobs = parse_message(body)
        # Conflicting versions of the same opportunity inside one alert are ambiguous.
        unique = {}
        for job in jobs:
            if job.key in unique and unique[job.key] != job:
                raise ValueError("Conflicting opportunity versions within one message")
            unique[job.key] = job
        return self.repository.ingest(
            message_id, fingerprint(body), [evaluate(job, self.profile) for job in unique.values()]
        )

    def draft(self, review_id: str) -> str | None:
        claimed, state, value = self.repository.claim(review_id)
        if not claimed:
            return value if state == "confirmed" else None
        try:
            receipt = self.provider.create(review_id, value)
            self.repository.finish(review_id, receipt)
            return receipt
        except BaseException:
            # The provider might have succeeded. Never automatically repeat this write.
            self.repository.finish(review_id, None)
            raise

    def reconcile(self, review_id: str) -> str | None:
        intent = self.repository.intent(review_id)
        if not intent:
            raise ValueError("No intent to reconcile")
        if intent[0] == "confirmed":
            return intent[1]
        receipt = self.provider.lookup(review_id)
        if receipt:
            self.repository.finish(review_id, receipt)
        return receipt
