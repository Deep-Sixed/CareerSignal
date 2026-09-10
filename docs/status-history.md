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

## Compare and append

An operator records a status because of a state they read. That state has to still hold when the write lands, or the decision is being applied to something else.

Suppose the operator reads event 41, `interested`, and decides `applied`. Before their write, another process records event 42, `withdrawn`. Appending `applied` now would bury a newer decision under an older one and make it current — silently, because append-only makes an overwrite impossible but does nothing about ordering.

So the operator-facing write names the event it was decided against:

```python
repository.record_status(
    opportunity_id, "applied", actor="operator", reason="Sent CV", expected_event_id=41
)
```

and the comparison happens inside the write transaction:

```
BEGIN IMMEDIATE
  latest event == expected?
      yes -> append
      no  -> refuse; the state moved, reevaluate
COMMIT
```

Inside, not before. Reading the latest event before opening the transaction would only move the race earlier: the value has to be read where nothing can commit between reading it and the insert that depends on it. A test asserts this by checking that the write reservation is already held at the moment the comparison reads.

A refusal writes nothing — not even a record of the refusal. The ledger is append-only and this is not an event; it is the decision not to write one. `StatusConflict` names the event that was expected and the event and status that were found, so the operator can read the current state and decide again.

This is the same rule an approval already follows before an outward draft: act against a known state, and refuse rather than proceed when the state has moved. It adds no new status, no lock and no reservation — only the refusal to write blindly.

`expected_event_id` is optional in the API because not every append is an operator acting on something they read: intake records the opening `new` inside the transaction that creates the opportunity, where there is no prior state to have moved. Every operator-facing write supplies it, and [the CLI](operator-interface.md#recording-a-status) has no path that omits it.

## Use

```python
recorded = repository.record_status(
    opportunity_id, "applied", actor="operator", reason="Sent CV", expected_event_id=41
)
recorded  # {'status': 'applied', 'event': 42, 'previous_event': 41}
repository.status(opportunity_id)  # 'applied'
repository.status_history(opportunity_id)  # [(status, actor, reason, created_at), ...]
```

The append returns the event it wrote rather than leaving the caller to look it up afterwards; by then another writer may have appended a newer one.

From a terminal:

```sh
careersignal status <opportunity-id> --to applied --expect 41 --actor operator --reason "Sent CV"
```

## Limitations

- **A blind append is still possible from inside the application.** `expected_event_id` is what makes a write safe, and a caller that omits it gets the old behaviour: two writers produce two events in the order the database serialized them and the later one is current. The operator interface never omits it, but the API cannot force it without also refusing the opening event that intake records.
- **An expectation is not a reservation.** It says the opportunity has not moved since the value was read. It does not stop a second operator from winning the race — only from winning it silently.
- **Duplicate consecutive statuses are allowed.** Recording `applied` twice records it twice. That is what happened, and collapsing it would be an edit.
- **The vocabulary is enforced in two places** — the domain module and a SQL `CHECK` — so the database refuses a bad value on its own. A test asserts the two lists stay equal, since nothing else would notice them drifting apart.
- **`reason` is free text** and can contain personal information at runtime. Keep the database private; public fixtures are synthetic.
