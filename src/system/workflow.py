"""Compose pure recruiting rules, real storage and controlled draft actions."""

from communications.controlled import parse_message
from communications.message import Message
from data.repository import Repository
from recruiting.extraction import PARSER_VERSION, extract, extract_records
from recruiting.models import Profile, evaluate, fingerprint
from recruiting.ports import DraftProvider, DraftRefused


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
        # A durable intent settles this before anything else is considered. Once an
        # external attempt has been made, its outcome is a fact about that attempt, and
        # data arriving afterwards cannot change what it was: a later message carrying a
        # hostile address must not turn a confirmed receipt, or an uncertain result waiting
        # to be reconciled, into a refusal about a draft that was never proposed.
        prior = self.repository.intent(review_id)
        if prior:
            # One review, one intent, and the intent already has a destination. A request
            # naming a different provider or mailbox is not this attempt replaying -- it is
            # a different destination asking for a review that already has one, and must
            # perform no new write. The comparison is declared identity only: a settled
            # attempt's outcome does not get re-verified against a network every time it is
            # replayed.
            bound = self.repository.intent_identity(review_id)
            requested = (self.provider.provider, self.provider.namespace)
            if bound is None or (bound["provider"], bound["provider_namespace"]) != requested:
                raise ValueError(
                    "Requested provider does not match the draft intent already reserved "
                    "for this review; reconcile using the original provider and mailbox"
                )
            return prior[1] if prior[0] == "confirmed" else None
        # An approval for one destination does not authorize a request for a different one,
        # however each has been typed -- checked here, before any local composition check
        # or external call, exactly the economy claim() already gives content and
        # addressing by re-verifying them, not re-deriving them, only where they matter. No
        # approval at all is not a mismatch to report here: claim() already says so, in
        # words this method does not need to duplicate.
        approval = self.repository.approved_identity(review_id)
        requested_provider, requested_namespace = self.provider.provider, self.provider.namespace
        if approval is not None and (approval["provider"], approval["provider_namespace"]) != (
            requested_provider,
            requested_namespace,
        ):
            raise ValueError(
                "Requested provider does not match the approval; approve this review again "
                "for the intended destination"
            )
        # Ask whether this draft can be composed at all before reserving anything. A
        # refusal is certain -- nothing was sent -- and recording it as an uncertain
        # external result would strand the review: the decision locks for reconciliation,
        # and reconciliation cannot find a draft that was never created. With nothing
        # reserved there is also nothing to unwind, which is a stronger guarantee than
        # releasing a claim afterwards would be.
        material = self.repository.draft_material(review_id)
        reason = self.provider.refusal(
            review_id, material["body"], to=material["to"], subject_line=material["subject"]
        )
        if reason:
            # refuse() decides this inside its own transaction. The read above is for
            # replay; this is the atomic guard on the refusal path, which never reaches
            # claim() and would otherwise have none. If an intent appeared in between, the
            # external attempt is authoritative and a later hostile source does not revise
            # it -- the same rule as at the top of this method, applied where it can hold.
            settled = self.repository.refuse(review_id)
            if settled:
                return settled["receipt"] if settled["state"] == "confirmed" else None
            raise DraftRefused(reason)
        # Proves what the credential actually is, not merely what the operator typed.
        # Called every time this point is reached -- even without an approval to check it
        # against yet -- so the value handed to claim() is always the provider's own answer
        # for what it is right now, and an approval that comes into existence in the
        # instant between the read above and the transaction below is still checked against
        # a genuinely verified identity rather than an unverified declaration.
        verified_namespace = self.provider.identity()
        if approval is not None and verified_namespace != approval["provider_namespace"]:
            raise ValueError(
                "Verified provider identity does not match the approval; approve this "
                "review again for the intended destination"
            )
        claim = self.repository.claim(
            review_id, provider=requested_provider, provider_namespace=verified_namespace
        )
        if not claim["claimed"]:
            # Reachable only if an intent appeared between the read above and this
            # transaction. The read is for replay; this is the atomic guard, and removing
            # either would leave a hole the other does not cover.
            return claim["receipt"] if claim["state"] == "confirmed" else None
        # Use exactly the material the claim transaction verified. Re-reading the address
        # here would reopen the window that check closes: a newer source arriving in the
        # interval would move the target of an already authorized write.
        approved = claim["material"]
        try:
            receipt = self.provider.create(
                review_id,
                approved["body"],
                to=approved["to"],
                subject_line=approved["subject"],
            )
            self.repository.finish(review_id, receipt)
            return receipt
        except BaseException:
            # Past this point the provider might have succeeded, so the outcome genuinely
            # is unknown. Never automatically repeat this write.
            self.repository.finish(review_id, None)
            raise

    def reconcile(self, review_id: str) -> str | None:
        intent = self.repository.intent(review_id)
        if not intent:
            raise ValueError("No intent to reconcile")
        # Bound the same way draft() binds a replay: declared identity first, checked
        # before any lookup and before any request. An uncertain attempt belonging to one
        # destination must not be reconciled using a different provider or a different
        # declared mailbox -- Gmail's own lookup would search a mailbox this attempt was
        # never made against.
        bound = self.repository.intent_identity(review_id)
        requested = (self.provider.provider, self.provider.namespace)
        if bound is None or (bound["provider"], bound["provider_namespace"]) != requested:
            raise ValueError(
                "Requested provider does not match the draft intent reserved for this "
                "review; reconcile using the original provider and mailbox"
            )
        if intent[0] == "confirmed":
            # A settled receipt is a fact about an attempt already made. Replaying it
            # needs no live check: unlike the lookup below, nothing here could search the
            # wrong mailbox, because nothing here searches at all.
            return intent[1]
        # The declared mailbox matching the intent is not proof the credential behind it
        # does: a credential that verifies as somebody else would otherwise have Gmail's
        # own lookup search a mailbox this attempt was never made against, silently. The
        # same live check draft() makes before claim() is made here before lookup().
        verified_namespace = self.provider.identity()
        if verified_namespace != bound["provider_namespace"]:
            raise ValueError(
                "Verified provider identity does not match the draft intent reserved for "
                "this review; reconcile using the original provider and mailbox"
            )
        receipt = self.provider.lookup(review_id)
        if receipt:
            self.repository.finish(review_id, receipt)
        return receipt
