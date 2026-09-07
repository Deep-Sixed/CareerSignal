"""Durable intake, version-bound decisions, draft intents and audit records."""

import json
from dataclasses import replace

from data.store import connection, migrate, transaction
from recruiting.models import Review, fingerprint


class Repository:
    def __init__(self, path):
        self.path = path
        migrate(path)

    def ingest(self, message_id: str, digest: str, reviews: list[Review]) -> list[str]:
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
                        "INSERT INTO reviews VALUES (?,?,?,?,?,?,?)",
                        (
                            review_id,
                            job.key,
                            review.id,
                            review.score,
                            int(review.advances),
                            json.dumps(packet.payload()),
                            review.draft,
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
            return sorted(result)

    def review(self, review_id):
        with connection(self.path) as conn:
            row = conn.execute("SELECT payload FROM reviews WHERE id=?", (review_id,)).fetchone()
            if not row:
                raise KeyError(review_id)
            return json.loads(row[0])

    @staticmethod
    def _current(conn, review_id):
        row = conn.execute(
            "SELECT r.advances FROM reviews r JOIN opportunities o "
            "ON o.current_review=r.id WHERE r.id=?",
            (review_id,),
        ).fetchone()
        if row is None:
            raise ValueError("Review is missing or stale")
        return bool(row[0])

    def decide(self, review_id, *, approved: bool, actor: str):
        if type(approved) is not bool or not actor.strip():
            raise ValueError("Explicit boolean decision and actor are required")
        with connection(self.path) as conn, transaction(conn):
            advances = self._current(conn, review_id)
            if approved and not advances:
                raise ValueError("Ineligible or below-threshold review cannot be approved")
            if conn.execute(
                "SELECT 1 FROM draft_intents WHERE review_id=?", (review_id,)
            ).fetchone():
                raise ValueError("Draft already attempted; decision is locked for reconciliation")
            conn.execute(
                "INSERT INTO decisions VALUES (?,?,?) ON CONFLICT(review_id) DO UPDATE SET "
                "approved=excluded.approved,actor=excluded.actor",
                (review_id, int(approved), actor.strip()),
            )
            conn.execute(
                "INSERT INTO audit(review_id,event) VALUES (?,?)",
                (review_id, "approved" if approved else "rejected"),
            )

    def claim(self, review_id):
        with connection(self.path) as conn, transaction(conn):
            self._current(conn, review_id)
            decision = conn.execute(
                "SELECT approved FROM decisions WHERE review_id=?", (review_id,)
            ).fetchone()
            if decision != (1,):
                raise ValueError("Explicit approval is required")
            prior = conn.execute(
                "SELECT state,receipt FROM draft_intents WHERE review_id=?", (review_id,)
            ).fetchone()
            if prior:
                return False, prior[0], prior[1]
            conn.execute(
                "INSERT INTO draft_intents(review_id,state) VALUES (?, 'attempting')", (review_id,)
            )
            conn.execute(
                "INSERT INTO audit(review_id,event) VALUES (?, 'draft_intent')", (review_id,)
            )
            body = conn.execute("SELECT draft FROM reviews WHERE id=?", (review_id,)).fetchone()[0]
            return True, "attempting", body

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

    def audit(self, review_id):
        with connection(self.path) as conn:
            return [
                r[0]
                for r in conn.execute(
                    "SELECT event FROM audit WHERE review_id=? ORDER BY id", (review_id,)
                ).fetchall()
            ]
