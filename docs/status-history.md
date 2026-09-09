# Status history

An opportunity carries a status the operator controls. The status is not stored on the opportunity: it is the latest event in an append-only history, so how a search arrived at its current state is never lost.

## Vocabulary

```
new  reviewing  interested  applied  interviewing  offer  rejected  withdrawn  closed
```

Only these are accepted, and a supplied value is normalized for case and surrounding whitespace before it is stored. Anything else is refused rather than invented, so the database never holds a status the product has no meaning for.

**No transition is forbidden.** Real searches jump: a referral can start at `interviewing`, an application can be withdrawn from any point, and a rejection can be revisited. The vocabulary is validated; the order is not a rule until there is a concrete reason for one.

Reopening a rejected opportunity means recording an active status again — `interested`, `reviewing` — rather than a separate `reopened` status. The history already shows what it was reopened from, so a distinct status would record the same fact twice and could disagree with itself.

## Append-only

`opportunity_status_history` holds `opportunity_id`, `status`, `actor`, `reason` and `created_at`. Triggers refuse `UPDATE` and `DELETE`, exactly as they do for `audit`. A correction is a new event; the earlier one stays.

The current status is the latest row **by id, not by timestamp**. Two events recorded in the same second share a `created_at`, and a backfilled or corrected row can carry an earlier one, so ordering by the clock can reorder the history and name the wrong current status. Nothing is ever deleted from this table, so ids stay monotonic and "latest" is unambiguous.

Because the current state is derived, every state the opportunity passed through can be reconstructed by replaying the history. There is no cached current status that could disagree with it.

## Opening and replay

Every opportunity is opened at `new` when it is first ingested, recorded as an event with actor `intake` rather than assumed from an absent row.

That write is idempotent by construction rather than by knowing whether the opportunity was newly inserted: it inserts only when the opportunity has no history at all. Replaying a message, or receiving a second message that carries the same job, adds nothing.

A status the operator recorded is never disturbed by a later intake. An opportunity re-offered by a new message keeps the status it has.

## What status is not

Status is a record of what the operator decided. It is not authorization.

- Recording a status creates no approval, no draft intent, and calls no provider. Drafting still requires an explicit `Repository.decide` against a specific review version.
- Recording a status changes nothing in `messages`, `opportunities`, `reviews`, `provenance`, `decisions`, `draft_intents`, `extraction_items` or `audit`.
- Nothing here writes to Gmail. The Gmail adapter remains read-only.

## Upgrading

Migration 0004 backfills a `new` event for every opportunity recorded before status history existed. Those rows name `migration` as the actor and say they were backfilled, so the record does not claim an operator was there.

## Use

```python
repository.record_status(opportunity_id, "applied", actor="operator", reason="Sent CV")
repository.status(opportunity_id)          # 'applied'
repository.status_history(opportunity_id)  # [(status, actor, reason, created_at), ...]
```

There is no CLI or operator view yet; that is deliberately the next piece of work rather than part of this one.

## Limitations

- **Concurrent recordings both persist.** Append-only makes an overwrite impossible, so two operators moving one opportunity at the same time produce two events in the order the database serialized them, and the later one is current. Nothing detects that the second operator was acting on a state the first had already changed; a compare-and-append guard would be a separate decision.
- **Duplicate consecutive statuses are allowed.** Recording `applied` twice records it twice. That is what happened, and collapsing it would be an edit.
- **The vocabulary is enforced in two places** — the domain module and a SQL `CHECK` — so the database refuses a bad value on its own. A test asserts the two lists stay equal, since nothing else would notice them drifting apart.
- **`reason` is free text** and can contain personal information at runtime. Keep the database private; public fixtures are synthetic.
