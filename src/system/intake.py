"""The one way incoming material enters CareerSignal, whichever surface asked for it.

Two surfaces now run intake -- the command line and the loopback web surface -- and there is
exactly one rule about how they may differ: they may not. Extraction, scoring, message
identity, deduplication and provenance are decided by `Message`, `Intake` and
`Repository.ingest`, and this module reaches those rather than reproducing any of them. A
browser that parsed its own MIME, or scored against a profile a page supplied, would be a
second CareerSignal with the same database and a different idea of what a job is.

So what lives here is acquisition and handoff, and nothing else:

    bytes or a mailbox  ->  Message  ->  Intake.intake_message  ->  Repository.ingest

The evaluation profile is a launch fact, handed in by whoever parsed the command line. It is
never a field of a request. Skills and accepted locations decide which opportunities advance,
and an incoming message is written by a recruiter -- material that could choose the policy it
is judged against is not material being judged.

Gmail is read whole before anything local is written. `GmailReader.messages()` already fetches
the entire bounded batch before returning, and that property is the reason this calls it
rather than looping fetch-and-write: a batch that fails at message N must leave nothing from
messages 1..N-1 behind, and no write may be held open across a network call.
"""

from communications.message import Message
from data.repository import Repository
from recruiting.models import Profile
from system.workflow import Intake

# What one Gmail read takes in when nothing says otherwise -- the same default the command
# line has always used. There is deliberately no ceiling here: `careersignal gmail-ingest` has
# never had one, and the bound that matters is the browser's, which `system.web` enforces on
# the one surface where a single button press could otherwise become a mailbox walk.
DEFAULT_GMAIL_BATCH = 25


class IntakeUnavailable(RuntimeError):
    """This launch was not configured with the authority the request needs.

    Not a refusal by the material and not a failure of the source: nothing was read, nothing
    was contacted, and nothing was written. It is a fact about how CareerSignal was started
    -- no evaluation profile, or no Gmail read credential -- and the operator fixes it by
    starting it differently rather than by correcting the request.
    """


class SourceUnreadable(RuntimeError):
    """The source was contacted and could not be read, so nothing was admitted.

    Declared by this boundary rather than deduced from an exception class, for the reason
    `DraftUncertain` exists on the outward side: a `ValueError` from a malformed command and a
    failure inside a Gmail read are the same class and opposite facts, and a surface that
    guessed would tell the operator to fix a request that was never wrong.

    Unlike the outward boundary's uncertainty, this one is certain in the direction that
    matters: it is raised only from the read phase, which happens in full before the first
    local write, so no message from the attempted batch was stored. The original is kept as
    `__cause__`; its text never carries the credential, because the adapter that raised it
    never puts one in a message.
    """


class IntakeActions:
    """Acquire messages and hand them to the intake that already exists.

    `gmail_reader` is optional and is the whole of this service's Gmail authority: a
    read-only `GmailReader`, built by the composition root that parsed the command line.
    Local `.eml` intake needs no credential at all, so a launch can legitimately have one
    source and not the other -- and the compose credential that creates drafts is a
    different grant entirely, which this service never sees.
    """

    def __init__(self, repository: Repository, profile: Profile, gmail_reader=None):
        self.repository, self.profile = repository, profile
        self.gmail_reader = gmail_reader
        # The existing composition, unchanged. Everything this service does to a message it
        # does by handing it here.
        self._intake = Intake(repository, profile)

    def available(self) -> dict:
        """What this launch can actually take in, answered without contacting anything.

        Discovery of CareerSignal's own configuration, not of a mailbox. `gmail.available`
        says a read credential was supplied at launch; whether Google still honours it is
        proven by the Gmail command itself, at the moment it matters, and probing here would
        make a read of local configuration depend on the network.

        No credential value, no length and no fragment of one appears in the result. The
        namespace is the mailbox name the operator declared, which is already reported by
        `/session` and is not a secret; it is present only where there is a reader to name.
        """
        gmail = {"available": self.gmail_reader is not None}
        if self.gmail_reader is not None:
            gmail["namespace"] = self.gmail_reader.namespace
        return {
            "eml": {"available": True},
            "gmail": gmail,
            "profile": {
                "skills": list(self.profile.skills),
                "locations": list(self.profile.locations),
            },
        }

    def ingest_eml(self, raw: bytes, namespace: str) -> dict:
        """One RFC822 message, from bytes the operator chose, under a namespace they declared.

        The namespace is provenance the operator asserts, and it is deliberately not derived
        from the message: a filename, a `From`, a `Message-ID` or a subject are all written by
        whoever sent the mail, and letting one name its own source would let a recruiter
        choose which mailbox CareerSignal believes their message arrived in.

        `Message.from_bytes` owns every question about the bytes -- the size ceiling, the MIME
        structure, which alternative is the body, whether a charset is usable -- and refuses
        with a `ValueError` the caller reports as a bad request. Nothing is parsed here.
        """
        message = Message.from_bytes(raw, namespace=namespace)
        reviews = self._intake.intake_message(message)
        return {
            "source": "eml",
            "namespace": message.namespace,
            "message": message.key,
            "external_id": message.external_id,
            "reviews": reviews,
            "diagnostics": self._diagnostics(message.key),
        }

    def ingest_gmail(self, query: str = "", label_ids=(), limit: int = DEFAULT_GMAIL_BATCH) -> dict:
        """Read a bounded Gmail batch in full, then take it in.

        The ordering is the contract, not an implementation detail:

            verify identity -> read the whole batch -> write locally

        `verify_identity()` proves whose mailbox the credential belongs to before a single
        message is admitted, because `--mailbox` is a declaration and Gmail serves whatever
        the token owns. A mismatch fails closed, here, before anything is stored under a
        namespace the credential does not answer to.

        `messages()` then returns the entire batch or raises, so a failure at message N leaves
        nothing from 1..N-1 written: the local writes below cannot begin until the network is
        finished with. Doing this as fetch-one-write-one would hold a write reservation across
        a network call and leave a partial import behind on any failure.

        Every failure from the reader is declared as `SourceUnreadable`, whatever class the
        adapter raised. The caller therefore knows, without inspecting anything, that the
        provider side failed and that nothing local was written.
        """
        if self.gmail_reader is None:
            raise IntakeUnavailable(
                "this launch has no Gmail read credential, so no mailbox was contacted"
            )
        # Shaped before the try, so a caller's own bad argument stays a caller's error. Inside
        # it, everything becomes `SourceUnreadable`, which is a claim about the provider side
        # that a TypeError raised here would not support.
        label_ids = tuple(label_ids)
        try:
            namespace = self.gmail_reader.verify_identity()
            messages = self.gmail_reader.messages(query=query, label_ids=label_ids, limit=limit)
        except Exception as exc:
            raise SourceUnreadable(f"the mailbox could not be read: {exc}") from exc
        return {
            "source": "gmail",
            "mailbox": namespace,
            "read": len(messages),
            "messages": [
                {
                    "external_id": message.external_id,
                    "message": message.key,
                    "reviews": self._intake.intake_message(message),
                    "diagnostics": self._diagnostics(message.key),
                }
                for message in messages
            ],
        }

    def _diagnostics(self, message_key: str) -> list:
        """What the extractor made of each item in one message, read back from storage.

        Read rather than remembered, and after the write rather than during it, so a replay
        reports exactly what a first import reported: `ingest()` returns the existing reviews
        for a message it already holds, and the evidence rows it stored the first time are
        still the answer.

        The excerpt is deliberately absent. It is recruiter-controlled text, it is already
        served by the communications projection an operator can open, and an intake receipt
        is a report about what happened rather than a second copy of the message.
        """
        return [
            {"item": row[0], "reason": row[2], "review": row[4]}
            for row in self.repository.extraction_evidence(message_key)
        ]
