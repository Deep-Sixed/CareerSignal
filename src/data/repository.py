"""Durable intake, version-bound decisions, draft intents and audit records."""

import json
from dataclasses import replace

from data.store import connection, migrate, transaction
from recruiting.models import Review, fingerprint
from recruiting.status import (
    ACTIVE,
    INITIAL,
    STATUSES,
    TERMINAL,
    StatusConflict,
    validate,
)


class Repository:
    def __init__(self, path):
        self.path = path
        migrate(path)

    def ingest(
        self, message_id: str, digest: str, reviews: list[Review], *, source=None, items=()
    ) -> list[str]:
        if not message_id.strip():
            raise ValueError("Message ID is required")
        with connection(self.path) as conn, transaction(conn):
            prior = conn.execute("SELECT digest FROM messages WHERE id=?", (message_id,)).fetchone()
            if prior:
                if prior[0] != digest:
                    raise ValueError("Message ID reused with different content")
                return [
                    r[0]
                    for r in conn.execute(
                        "SELECT review_id FROM provenance WHERE message_id=? ORDER BY review_id",
                        (message_id,),
                    ).fetchall()
                ]
            conn.execute("INSERT INTO messages VALUES (?,?)", (message_id, digest))
            result = set()
            for review in reviews:
                job = review.opportunity
                conn.execute(
                    "INSERT OR IGNORE INTO opportunities(id,url,title,company,location) "
                    "VALUES (?,?,?,?,?)",
                    (job.key, job.url, job.title, job.company, job.location),
                )
                # Idempotent by construction rather than by knowing whether the insert
                # above fired: replaying a message, or a second message carrying the same
                # job, must not append another opening event.
                conn.execute(
                    "INSERT INTO opportunity_status_history(opportunity_id,status,actor,reason) "
                    "SELECT ?,?,'intake','' WHERE NOT EXISTS "
                    "(SELECT 1 FROM opportunity_status_history WHERE opportunity_id=?)",
                    (job.key, INITIAL, job.key),
                )
                current = conn.execute(
                    "SELECT r.id,r.content_digest FROM reviews r JOIN opportunities o "
                    "ON o.current_review=r.id WHERE o.id=?",
                    (job.key,),
                ).fetchone()
                if current and current[1] == review.id:
                    review_id = current[0]
                else:
                    review_id = fingerprint([review.id, current[0] if current else None])
                    packet = replace(review, id=review_id)
                    conn.execute(
                        "INSERT INTO reviews(id,opportunity_id,content_digest,score,advances,"
                        "payload,draft,stated_skills,matched_skills,coverage) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?)",
                        (
                            review_id,
                            job.key,
                            review.id,
                            review.score or 0,
                            int(review.advances),
                            json.dumps(packet.payload()),
                            review.draft,
                            review.coverage[1],
                            review.coverage[0],
                            review.score,
                        ),
                    )
                    conn.execute(
                        "UPDATE opportunities SET current_review=?,title=?,company=?,location=? "
                        "WHERE id=?",
                        (review_id, job.title, job.company, job.location, job.key),
                    )
                    conn.execute(
                        "INSERT INTO audit(review_id,event) VALUES (?, 'review_created')",
                        (review_id,),
                    )
                conn.execute(
                    "INSERT OR IGNORE INTO provenance VALUES (?,?,?)",
                    (message_id, job.key, review_id),
                )
                result.add(review_id)
            if source is not None:
                conn.execute(
                    "INSERT INTO message_sources VALUES (?,?,?,?,?,?,?)",
                    (
                        message_id,
                        source["namespace"],
                        source["external_id"],
                        source["sender"],
                        source["subject"],
                        source["format"],
                        source["parser_version"],
                    ),
                )
            for item in items:
                job_key = item.opportunity.key if item.opportunity else None
                review_key = (
                    conn.execute(
                        "SELECT current_review FROM opportunities WHERE id=?", (job_key,)
                    ).fetchone()[0]
                    if job_key
                    else None
                )
                conn.execute(
                    "INSERT INTO extraction_items VALUES (?,?,?,?,?,?)",
                    (message_id, item.index, item.excerpt, item.reason, job_key, review_key),
                )
            return sorted(result)

    def extraction_evidence(self, message_id):
        """Return persisted diagnostics and exact per-item review links, including replay."""
        with connection(self.path) as conn:
            return conn.execute(
                "SELECT item_index,excerpt,reason,opportunity_id,review_id "
                "FROM extraction_items WHERE message_id=? ORDER BY item_index",
                (message_id,),
            ).fetchall()

    # One row per opportunity: the durable business object. The review is evidence about
    # it, joined in as columns rather than being the subject of the list.
    SUMMARY = (
        "SELECT o.id,o.company,o.title,o.location,o.url,s.status,s.created_at,"
        "r.id,r.coverage,r.stated_skills,r.matched_skills,r.advances,"
        "json_extract(r.payload,'$.eligible'),s.id "
        "FROM opportunities o "
        "LEFT JOIN reviews r ON r.id=o.current_review "
        "LEFT JOIN opportunity_status_history s ON s.id="
        "(SELECT h.id FROM opportunity_status_history h "
        "WHERE h.opportunity_id=o.id ORDER BY h.id DESC LIMIT 1)"
    )

    @staticmethod
    def _summary(row) -> dict:
        stated = row[9]
        return {
            "id": row[0],
            "company": row[1],
            "title": row[2],
            "location": row[3],
            "url": row[4],
            "status": row[5],
            "status_changed_at": row[6],
            "review": row[7],
            "coverage": row[8],
            "matched_skills": row[10],
            "stated_skills": stated,
            "advances": None if row[11] is None else bool(row[11]),
            # The event the status was read from. An operator recording the next status
            # names it, so that a decision taken against this state cannot land on top of
            # one recorded in between. Zero means no history at all.
            "status_event": row[13] or 0,
            "eligible": None if row[12] is None else bool(row[12]),
            # A review written before stated skill coverage keeps NULL in these columns and
            # cannot be approved or drafted. The operator needs to see that, not a blank.
            "scored": None if row[7] is None else stated not in (None, 0),
            "actionable": None if row[7] is None else stated is not None,
        }

    def opportunities(
        self, *, status=None, active=False, eligible=None, min_coverage=None, max_coverage=None
    ) -> list[dict]:
        """Read-only. Nothing here writes, decides, drafts, or contacts anything."""
        clauses, values = [], []
        if status is not None:
            clauses.append("s.status=?")
            values.append(validate(status))
        if active:
            clauses.append("s.status IN (" + ",".join("?" * len(ACTIVE)) + ")")
            values.extend(ACTIVE)
        if eligible is not None:
            if type(eligible) is not bool:
                raise ValueError("Eligible must be a boolean")
            clauses.append("json_extract(r.payload,'$.eligible')=?")
            values.append(int(eligible))
        for name, bound, comparison in (
            ("min_coverage", min_coverage, ">="),
            ("max_coverage", max_coverage, "<="),
        ):
            if bound is None:
                continue
            if type(bound) is not int or not 0 <= bound <= 100:
                raise ValueError(f"{name} must be a whole number between 0 and 100")
            clauses.append(f"r.coverage{comparison}?")
            values.append(bound)
        sql = self.SUMMARY + (" WHERE " + " AND ".join(clauses) if clauses else "")
        with connection(self.path) as conn:
            rows = [self._summary(row) for row in conn.execute(sql, tuple(values)).fetchall()]
        # Ordered in Python because the pipeline order is the domain's vocabulary, which the
        # database does not know: stage first, then best covered, then company for stability.
        order = {name: index for index, name in enumerate(STATUSES)}
        return sorted(
            rows,
            key=lambda row: (
                order.get(row["status"], len(order)),
                -(row["coverage"] if row["coverage"] is not None else -1),
                row["company"].casefold(),
                row["title"].casefold(),
            ),
        )

    @staticmethod
    def _action(conn, review_id, binding) -> dict:
        """What has been decided and attempted for this review, in the operator's terms.

        Reporting only. The write paths decide for themselves whether an action is still
        authorized -- claim() rechecks every binding inside its own transaction -- so this
        is what has happened, not a prediction of what would be allowed next.
        """
        if review_id is None:
            return {
                "decision": None,
                "actor": None,
                "binds": None,
                "attempted": False,
                "draft": "none",
                "receipt": None,
                "provider": None,
                "provider_namespace": None,
            }
        decision = conn.execute(
            "SELECT approved,actor,addressing_digest,draft_digest,provider,provider_namespace "
            "FROM decisions WHERE review_id=?",
            (review_id,),
        ).fetchone()
        intent = conn.execute(
            "SELECT state,receipt FROM draft_intents WHERE review_id=?", (review_id,)
        ).fetchone()
        # The most recent of the events that can change what the operator may do next.
        # Asking whether a refusal has ever been recorded would report a refusal that a
        # later approval has already answered: the decisions row is upserted, so a
        # reapproval replaces the decision while the old audit row stays -- correctly,
        # since audit is history. Which of the two is current is decided by their order,
        # and audit ids are append-only, so the latest one is the current action state.
        latest = conn.execute(
            "SELECT event FROM audit WHERE review_id=? AND "
            "event IN ('approved','rejected','draft_refused') ORDER BY id DESC LIMIT 1",
            (review_id,),
        ).fetchone()
        return {
            "decision": None if decision is None else ("approved" if decision[0] else "rejected"),
            "actor": decision[1] if decision else None,
            # Whether the recorded decision binds the material being shown beside it. A
            # later message can move the recipient and subject without touching anything
            # else, and the decision row stays exactly as it was recorded -- correctly, it
            # is the record of what was approved. Reporting it as current next to material
            # it does not bind would tell the operator they have authorized something they
            # have not. Compared against the same binding the packet was read from, so the
            # two describe one moment. A decision predating the addressing binding carries
            # an empty digest, which no real digest equals, so it reads as not binding.
            "binds": None
            if decision is None
            else (
                decision[2] == binding["addressing"]["digest"]
                and decision[3] == binding["draft_digest"]
            ),
            # Refused and uncertain are different facts and are never collapsed. A refusal
            # means nothing was attempted; an intent means something was, and its outcome
            # is a fact about that attempt. So an intent wins absolutely: the refusal
            # describes a draft that was never proposed, not the current state.
            # Whether a durable intent exists at all, taken from the row rather than
            # inferred from the state name. Once one does, decide() refuses every further
            # decision, so a reader that describes what the operator may do next has to
            # know this without keeping its own copy of which states imply it.
            "attempted": intent is not None,
            "draft": intent[0]
            if intent
            else ("refused" if latest and latest[0] == "draft_refused" else "none"),
            "receipt": intent[1] if intent else None,
            # The destination this decision names, straight from the row. Not folded into
            # "binds": a request for a different provider or mailbox is a mismatch that
            # workflow.draft() refuses outright, not a drift in the review's own material
            # that this approval could still be shown as authorizing. An empty value here
            # is a decision that predates this binding, and is shown as none rather than
            # guessed at.
            "provider": decision[4] or None if decision else None,
            "provider_namespace": decision[5] or None if decision else None,
        }

    @staticmethod
    def _bound(review_id, binding) -> dict | None:
        """Exactly the material an approval binds, read the way the approval reads it.

        Deliberately built from _binding(), the same statement decide() and claim() bind
        and re-verify from, rather than assembled from separate lookups. The operator has
        to be shown what will actually be authorized: a view that reconstructed the
        recipient by its own route could agree with the write path today and drift from it
        later, and the drift would be invisible precisely where it matters most.

        The wording comes from the reviews.draft column for the same reason. The payload
        carries a copy of it, written from the same value at intake, but the copy is not
        what the approval's draft digest is taken over.

        Returns None when the opportunity has no current review, which is also when there
        is nothing an approval could bind.
        """
        if review_id is None:
            return None
        return {
            "review": review_id,
            "source": binding["addressing"]["source"],
            "to": binding["addressing"]["to"],
            "subject": binding["addressing"]["subject"],
            "wording": binding["draft"],
        }

    def opportunity(self, opportunity_id) -> dict:
        """One opportunity with its current review packet and its whole status history."""
        with connection(self.path) as conn:
            row = conn.execute(self.SUMMARY + " WHERE o.id=?", (opportunity_id,)).fetchone()
            if row is None:
                raise KeyError(opportunity_id)
            summary = self._summary(row)
            packet = conn.execute(
                "SELECT payload FROM reviews WHERE id=?", (summary["review"],)
            ).fetchone()
            history = conn.execute(
                "SELECT status,actor,reason,created_at,id FROM opportunity_status_history "
                "WHERE opportunity_id=? ORDER BY id",
                (opportunity_id,),
            ).fetchall()
            # One read of the binding, shared by the packet and by the approval state, so
            # that what is shown and what is said about it describe the same moment.
            binding = (
                self._binding(conn, summary["review"]) if summary["review"] is not None else None
            )
            action = self._action(conn, summary["review"], binding)
            bound = self._bound(summary["review"], binding)
        return {
            **summary,
            "packet": json.loads(packet[0]) if packet else None,
            "action": action,
            "bound": bound,
            "history": [
                {"status": h[0], "actor": h[1], "reason": h[2], "created_at": h[3], "event": h[4]}
                for h in history
            ],
        }

    @staticmethod
    def _latest_event(conn, opportunity_id):
        """The id and status of the newest event, or (0, "") when there is no history.

        Zero is the unbound value here for the same reason it is in _binding: no row can
        carry it, so nothing an operator supplies compares equal to it by accident.
        """
        row = conn.execute(
            "SELECT id,status FROM opportunity_status_history WHERE opportunity_id=? "
            "ORDER BY id DESC LIMIT 1",
            (opportunity_id,),
        ).fetchone()
        return (row[0], row[1]) if row else (0, "")

    def record_status(
        self, opportunity_id, status, *, actor, reason="", expected_event_id=None
    ) -> dict:
        """Append a status event. Nothing is edited, so a correction is another event.

        Returns the status recorded and the id of the event that recorded it. The id is
        returned rather than looked up afterwards because the caller needs the event it
        actually appended: reading "the latest event" after the transaction would name a
        different row if another writer appended in between.

        `expected_event_id` makes this a compare-and-append. The operator reads a state,
        decides against it, and names the event they read; if the newest event is no longer
        that one, the opportunity moved under the decision and nothing is written. That is
        the same rule the draft path applies to an approval: act against a known state, and
        refuse rather than proceed when the state has moved.

        It stays optional because not every append is an operator acting on something they
        read. Intake records the opening `new` inside the transaction that creates the
        opportunity, where there is no prior state to have moved. Every operator-facing
        write supplies it -- the CLI has no path that omits it -- because a blind append
        from a surface that just printed a status is exactly the race this closes.
        """
        value = validate(status)
        if not isinstance(actor, str) or not actor.strip():
            raise ValueError("An actor is required")
        if not isinstance(reason, str):
            raise ValueError("Reason must be text")
        if expected_event_id is not None and (
            type(expected_event_id) is not int or expected_event_id <= 0
        ):
            raise ValueError("Expected event id must be a positive whole number")
        with connection(self.path) as conn, transaction(conn):
            if not conn.execute(
                "SELECT 1 FROM opportunities WHERE id=?", (opportunity_id,)
            ).fetchone():
                raise ValueError("Unknown opportunity")
            # Read and compare inside the write transaction. Checking before it would only
            # move the race earlier: the value has to be read where it cannot change before
            # the insert lands.
            observed, current = self._latest_event(conn, opportunity_id)
            if expected_event_id is not None and observed != expected_event_id:
                raise StatusConflict(expected_event_id, observed, current)
            conn.execute(
                "INSERT INTO opportunity_status_history(opportunity_id,status,actor,reason) "
                "VALUES (?,?,?,?)",
                # The reason is the operator's own prose. Only surrounding whitespace is
                # removed, because a stray trailing newline is noise but the line breaks
                # inside a note are theirs to keep.
                (opportunity_id, value, actor.strip(), reason.strip()),
            )
            event = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        return {"status": value, "event": event, "previous_event": observed}

    def status(self, opportunity_id):
        """Derived from history, never stored: the latest event is the current state."""
        with connection(self.path) as conn:
            row = conn.execute(
                "SELECT status FROM opportunity_status_history WHERE opportunity_id=? "
                "ORDER BY id DESC LIMIT 1",
                (opportunity_id,),
            ).fetchone()
            return row[0] if row else None

    def status_history(self, opportunity_id):
        with connection(self.path) as conn:
            return conn.execute(
                "SELECT status,actor,reason,created_at FROM opportunity_status_history "
                "WHERE opportunity_id=? ORDER BY id",
                (opportunity_id,),
            ).fetchall()

    def review(self, review_id):
        with connection(self.path) as conn:
            row = conn.execute("SELECT payload FROM reviews WHERE id=?", (review_id,)).fetchone()
            if not row:
                raise KeyError(review_id)
            return json.loads(row[0])

    @staticmethod
    def _current(conn, review_id):
        row = conn.execute(
            "SELECT r.advances,r.stated_skills FROM reviews r JOIN opportunities o "
            "ON o.current_review=r.id WHERE r.id=?",
            (review_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Review is missing or stale")
        # A review written before stated skill coverage carries an advances flag decided by a
        # formula this version does not use, and migration cannot recompute it without the
        # profile of the day. stated_skills is NULL only for those rows; a scoring-v2 review
        # of a job that stated no skills records 0. Refuse to act on the old decision rather
        # than honour it or rewrite the recorded reasoning.
        if row[1] is None:
            raise ValueError(
                "Review predates stated skill coverage and is not actionable; re-ingest the "
                "opportunity under a new message ID to score it again"
            )
        return bool(row[0])

    # The review's own digest, the exact wording, and the status the opportunity was in.
    # Read in one statement so the three cannot describe different moments.
    BINDING = (
        "SELECT r.content_digest,r.draft,o.id,"
        "(SELECT h.id FROM opportunity_status_history h WHERE h.opportunity_id=o.id "
        "ORDER BY h.id DESC LIMIT 1),"
        "(SELECT h.status FROM opportunity_status_history h WHERE h.opportunity_id=o.id "
        "ORDER BY h.id DESC LIMIT 1) "
        "FROM reviews r JOIN opportunities o ON o.current_review=r.id WHERE r.id=?"
    )

    @classmethod
    def _binding(cls, conn, review_id):
        row = conn.execute(cls.BINDING, (review_id,)).fetchone()
        if row is None:
            raise ValueError("Review is missing or stale")
        content_digest, draft, opportunity_id, event_id, status = row
        return {
            "addressing": cls._addressing(conn, review_id),
            "content_digest": content_digest,
            "draft_digest": fingerprint(draft),
            "opportunity_id": opportunity_id,
            # An opportunity always has an opening event, so this is only None for a row
            # that predates status history and was somehow not backfilled. Zero is the
            # unbound value, and nothing compares equal to it.
            "status_event_id": event_id or 0,
            "status": status or "",
            "draft": draft,
        }

    def decide(
        self,
        review_id,
        *,
        approved: bool,
        actor: str,
        provider: str = "controlled",
        provider_namespace: str = "controlled",
    ):
        if type(approved) is not bool or not actor.strip():
            raise ValueError("Explicit boolean decision and actor are required")
        if not provider.strip() or not provider_namespace.strip():
            raise ValueError("A provider and a provider namespace are required")
        with connection(self.path) as conn, transaction(conn):
            advances = self._current(conn, review_id)
            if approved and not advances:
                raise ValueError("Ineligible or below-threshold review cannot be approved")
            binding = self._binding(conn, review_id)
            # Approving an opportunity the operator has already ended would record an
            # authorization that can never be used. Refuse now rather than at draft time.
            if approved and binding["status"] in TERMINAL:
                raise ValueError(
                    f"Opportunity status is {binding['status']}; record an active status "
                    "before approving an outward draft"
                )
            if conn.execute(
                "SELECT 1 FROM draft_intents WHERE review_id=?", (review_id,)
            ).fetchone():
                raise ValueError("Draft already attempted; decision is locked for reconciliation")
            conn.execute(
                "INSERT INTO decisions(review_id,approved,actor,content_digest,draft_digest,"
                "status_event_id,addressing_digest,source_message_id,provider,"
                "provider_namespace) "
                "VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(review_id) DO UPDATE SET "
                "approved=excluded.approved,actor=excluded.actor,"
                "content_digest=excluded.content_digest,draft_digest=excluded.draft_digest,"
                "status_event_id=excluded.status_event_id,"
                "addressing_digest=excluded.addressing_digest,"
                "source_message_id=excluded.source_message_id,"
                "provider=excluded.provider,provider_namespace=excluded.provider_namespace",
                (
                    review_id,
                    int(approved),
                    actor.strip(),
                    binding["content_digest"],
                    binding["draft_digest"],
                    binding["status_event_id"],
                    binding["addressing"]["digest"],
                    binding["addressing"]["source"] or None,
                    provider.strip(),
                    provider_namespace.strip(),
                ),
            )
            conn.execute(
                "INSERT INTO audit(review_id,event) VALUES (?,?)",
                (review_id, "approved" if approved else "rejected"),
            )

    def approved_identity(self, review_id):
        """The provider and namespace the current approval binds, or None.

        A request naming a different destination can be refused here, before any local
        composition check or external identity call is made -- the same economy claim()
        already gives the content and addressing bindings by re-verifying them, not
        re-deriving them, only at the moment they matter. None is returned for a review
        with no decision, a rejection, or an approval that predates this binding: none of
        those authorize an outward draft to any destination, and the caller's existing
        approval-required refusal is what should say so.
        """
        with connection(self.path) as conn:
            row = conn.execute(
                "SELECT approved,provider,provider_namespace FROM decisions WHERE review_id=?",
                (review_id,),
            ).fetchone()
        if not row or row[0] != 1 or not row[1] or not row[2]:
            return None
        return {"provider": row[1], "provider_namespace": row[2]}

    def claim(
        self, review_id, *, provider: str = "controlled", provider_namespace: str = "controlled"
    ):
        with connection(self.path) as conn, transaction(conn):
            self._current(conn, review_id)
            decision = conn.execute(
                "SELECT approved,content_digest,draft_digest,addressing_digest,provider,"
                "provider_namespace FROM decisions WHERE review_id=?",
                (review_id,),
            ).fetchone()
            if not decision or decision[0] != 1:
                raise ValueError("Explicit approval is required")
            prior = conn.execute(
                "SELECT state,receipt FROM draft_intents WHERE review_id=?", (review_id,)
            ).fetchone()
            if prior:
                return {
                    "claimed": False,
                    "state": prior[0],
                    "receipt": prior[1],
                    "material": None,
                }
            # Everything below re-checks, inside this transaction, what the approval was
            # for. The external write happens after the transaction commits, so this is the
            # last moment the authorization can be decided against a state that cannot move
            # underneath it.
            binding = self._binding(conn, review_id)
            if not all(decision[index] for index in (1, 2, 3, 4, 5)):
                raise ValueError(
                    "Approval predates draft authorization binding and cannot authorize an "
                    "outward draft; approve this review again"
                )
            if decision[1] != binding["content_digest"]:
                raise ValueError(
                    "Review changed since it was approved; approve the current review again"
                )
            if decision[2] != binding["draft_digest"]:
                raise ValueError(
                    "Approved draft wording changed since it was approved; approve it again"
                )
            if binding["status"] in TERMINAL:
                raise ValueError(
                    f"Opportunity status is {binding['status']}; an ended opportunity does "
                    "not authorize an outward draft"
                )
            # An approval authorizes an outward action, and an outward action has a target.
            # A newer source can move that target without changing anything else about the
            # review, so the address is checked here too -- inside the same transaction, and
            # from the same query the approval was bound from.
            if decision[3] != binding["addressing"]["digest"]:
                raise ValueError("Addressing changed since approval; review and approve again")
            # The identity verified before this transaction is the identity that must still
            # be approved once inside it. A re-approval for a different destination landing
            # in between must not let the write it authorized be attributed to this one.
            if (decision[4], decision[5]) != (provider, provider_namespace):
                raise ValueError(
                    "Approved provider or mailbox changed since approval; approve this "
                    "review again for the intended destination"
                )
            conn.execute(
                "INSERT INTO draft_intents(review_id,state,status_event_id,source_message_id,"
                "provider,provider_namespace) VALUES (?, 'attempting', ?, ?, ?, ?)",
                (
                    review_id,
                    binding["status_event_id"],
                    binding["addressing"]["source"] or None,
                    provider,
                    provider_namespace,
                ),
            )
            conn.execute(
                "INSERT INTO audit(review_id,event) VALUES (?, 'draft_intent')", (review_id,)
            )
            # The material is returned rather than re-read afterwards. Re-querying the
            # address after this transaction would reopen the window this check closes: the
            # value verified here must be the value that is actually sent.
            return {
                "claimed": True,
                "state": "attempting",
                "receipt": None,
                "material": {
                    "body": binding["draft"],
                    "to": binding["addressing"]["to"],
                    "subject": binding["addressing"]["subject"],
                    "source": binding["addressing"]["source"],
                },
            }

    def finish(self, review_id, receipt: str | None):
        if receipt is not None and not receipt.strip():
            raise ValueError("Receipt cannot be empty")
        with connection(self.path) as conn, transaction(conn):
            prior = conn.execute(
                "SELECT state,receipt FROM draft_intents WHERE review_id=?", (review_id,)
            ).fetchone()
            if not prior:
                raise ValueError("No draft intent exists")
            if prior[0] == "confirmed":
                if receipt is not None and prior[1] != receipt:
                    raise ValueError("Receipt conflict")
                return
            state = "confirmed" if receipt else "uncertain"
            conn.execute(
                "UPDATE draft_intents SET state=?,receipt=? WHERE review_id=?",
                (state, receipt, review_id),
            )
            conn.execute(
                "INSERT INTO audit(review_id,event) VALUES (?,?)", (review_id, "draft_" + state)
            )

    def intent(self, review_id):
        with connection(self.path) as conn:
            return conn.execute(
                "SELECT state,receipt FROM draft_intents WHERE review_id=?", (review_id,)
            ).fetchone()

    def intent_identity(self, review_id):
        """The provider and namespace bound to this review's draft intent, or None.

        None means either there is no intent at all, or -- for an intent written before
        this binding existed -- that nothing records which provider or mailbox it was for.
        Both cases must refuse a request naming any destination, rather than assume one:
        this is the fact a settled attempt is compared against before it is ever replayed
        or reconciled, and inventing what a legacy row was for would be exactly the
        fabrication the surrounding contract refuses to do.
        """
        with connection(self.path) as conn:
            row = conn.execute(
                "SELECT provider,provider_namespace FROM draft_intents WHERE review_id=?",
                (review_id,),
            ).fetchone()
        if not row or not row[0] or not row[1]:
            return None
        return {"provider": row[0], "provider_namespace": row[1]}

    @staticmethod
    def _addressing(conn, review_id):
        """Who a draft would be addressed to, and which message said so.

        One query, used by the reader and by the authorization check alike, so the value an
        approval is bound to cannot be computed differently from the value that is used.

        One opportunity can arrive in more than one message, and the review is reused when
        the job has not changed, so a review can have several sources. The most recently
        ingested one wins: ordering by message_id would pick by hash, which means the
        recipient of an outward draft would be chosen arbitrarily and re-ingesting a
        corrected message might or might not take effect. Order by the row the insert
        assigned, for the same reason status history orders by id rather than by a
        timestamp: it is the arrival sequence.
        """
        row = conn.execute(
            "SELECT s.message_id,s.sender,s.subject FROM reviews r "
            "JOIN provenance p ON p.review_id=r.id "
            "JOIN message_sources s ON s.message_id=p.message_id "
            "WHERE r.id=? ORDER BY p.rowid DESC LIMIT 1",
            (review_id,),
        ).fetchone()
        source, to, subject = row if row else ("", "", "")
        # The digest covers the sender and the subject, which are what a draft is addressed
        # with. The message id is provenance and is deliberately outside it: the same target
        # arriving in a second message is not a materially different outward action.
        return {
            "source": source,
            "to": to,
            "subject": subject,
            "digest": fingerprint([to, subject]),
        }

    def addressing(self, review_id):
        """Who a draft would be addressed to, from the message the opportunity came from.

        Both values are recruiter-supplied and are returned exactly as stored. Deciding
        whether they can safely become headers belongs to the composition boundary, not
        here: the record of what arrived must not be quietly rewritten to make it sendable.
        """
        with connection(self.path) as conn:
            return self._addressing(conn, review_id)

    def draft_material(self, review_id):
        """Everything a draft would be composed from, read without reserving anything.

        Deliberately separate from claim(): the provider has to be able to refuse a draft
        before any durable intent exists, because a refusal must not consume the
        operator's approval. The draft body is re-checked against the approval inside
        claim(), so reading it here cannot authorize a stale one.
        """
        with connection(self.path) as conn:
            binding = self._binding(conn, review_id)
        return {"body": binding["draft"], **binding["addressing"]}

    def refuse(self, review_id):
        """Record that a draft was refused before anything left this machine.

        Returns the settled intent if one already exists, and records nothing in that case;
        returns None once the refusal is recorded.

        No intent row is ever written here. That is the whole point: an intent means an
        external write was attempted and its outcome may be unknown, and here it is known
        that nothing was sent. Writing one would strand the review -- the decision locks
        for reconciliation, and reconciliation cannot find a draft that was never created.

        The check is inside this transaction rather than left to the caller's earlier read,
        because the refusal path never reaches claim() and so has no other atomic guard. An
        intent appearing between that read and this write means an external attempt became
        authoritative first, and data arriving afterwards must not revise what it was.
        """
        with connection(self.path) as conn, transaction(conn):
            prior = conn.execute(
                "SELECT state,receipt FROM draft_intents WHERE review_id=?", (review_id,)
            ).fetchone()
            if prior:
                return {"state": prior[0], "receipt": prior[1]}
            conn.execute(
                "INSERT INTO audit(review_id,event) VALUES (?, 'draft_refused')", (review_id,)
            )
            return None

    def authorization(self, review_id):
        """What an approval for this review is bound to, and what is true now."""
        with connection(self.path) as conn:
            decision = conn.execute(
                "SELECT approved,actor,content_digest,draft_digest,status_event_id,"
                "addressing_digest,source_message_id,provider,provider_namespace "
                "FROM decisions WHERE review_id=?",
                (review_id,),
            ).fetchone()
            binding = self._binding(conn, review_id)
            return {
                "approved": bool(decision[0]) if decision else None,
                "actor": decision[1] if decision else None,
                "bound_content": decision[2] if decision else None,
                "bound_draft": decision[3] if decision else None,
                "bound_event": decision[4] if decision else None,
                "bound_addressing": decision[5] if decision else None,
                "bound_source": decision[6] if decision else None,
                "bound_provider": decision[7] if decision else None,
                "bound_provider_namespace": decision[8] if decision else None,
                "addressing_digest": binding["addressing"]["digest"],
                "source": binding["addressing"]["source"],
                "content_digest": binding["content_digest"],
                "draft_digest": binding["draft_digest"],
                "status": binding["status"],
                "status_event_id": binding["status_event_id"],
            }

    def audit(self, review_id):
        with connection(self.path) as conn:
            return [
                r[0]
                for r in conn.execute(
                    "SELECT event FROM audit WHERE review_id=? ORDER BY id", (review_id,)
                ).fetchall()
            ]
