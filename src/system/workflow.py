"""Compose pure recruiting rules, real storage and controlled draft actions."""

from communications.controlled import parse_message
from communications.message import Message
from data.repository import Repository
from recruiting.extraction import PARSER_VERSION, extract, extract_records
from recruiting.models import Profile, evaluate, fingerprint
from recruiting.ports import DraftProvider


class Workflow:
    def __init__(self, repository: Repository, provider: DraftProvider, profile: Profile):
        self.repository, self.provider, self.profile = repository, provider, profile

    def intake_message(self, message: Message) -> list[str]:
        items = extract(message.content)
        unique = {item.opportunity.key: item.opportunity for item in items if item.opportunity}
        return self.repository.ingest(
            message.key,
            message.digest,
            [evaluate(job, self.profile) for job in unique.values()],
            source={
                "namespace": message.namespace,
                "external_id": message.external_id,
                "sender": message.sender,
                "subject": message.subject,
                "format": message.format,
                "parser_version": PARSER_VERSION,
            },
            items=items,
        )

    def intake(self, message_id: str, body: str) -> list[str]:
        items = extract_records(parse_message(body))
        unique = {item.opportunity.key: item.opportunity for item in items if item.opportunity}
        return self.repository.ingest(
            message_id,
            fingerprint(body),
            [evaluate(job, self.profile) for job in unique.values()],
            items=items,
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
