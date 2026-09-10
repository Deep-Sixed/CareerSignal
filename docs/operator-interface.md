# Operator interface

CareerSignal is operated from a terminal. These commands are a boundary over what the application already does — they read what is stored and invoke the machinery that already owns each decision. Nothing here holds a rule of its own, and no command can reach an external mailbox by a route the approval machinery does not run through.

## What a command reports

The commands an operator reads — `opportunities`, `opportunity`, `status`, `approve`, `reject`, `draft`, `reconcile` — print text by default and structured output behind `--json`. The ones that act also report an outcome:

| Reported | Exit | Means |
|---|---|---|
| `ACCEPTED` | 0 | The thing asked for happened. |
| `REFUSED` | 1 | It did not happen, and this machine decided that. Nothing left the machine; correct what the refusal names and ask again. |
| `UNCERTAIN` | 3 | A provider was contacted and the outcome is unknown. Reconcile; never repeat the write. |

Argparse keeps exit 2 for a command that was written wrongly — a missing argument, an unknown id, a filter that cannot mean anything — and writes it to stderr. An outcome goes to stdout, because it is the answer rather than a diagnostic.

An id that names nothing is a mistyped command, so every command that takes one refuses it that way: an unknown opportunity id for `opportunity` and `status`, an unknown review id for `approve`, `reject`, `draft` and `reconcile`. A review that **exists** but cannot be acted on — approved and then changed, already attempted, an ended opportunity — is a refusal, exit 1, because that is the system deciding rather than the operator mistyping.

**Refused and uncertain are never collapsed into one failure.** They call for different moves. A refusal means nothing was created, the operator's approval survives, and correcting the cause and retrying is safe. An uncertain result means a draft may exist in the mailbox already, and only reconciliation can say. A script that treated them alike would either abandon something merely refused or duplicate something that already exists.

`UNCERTAIN` is therefore reserved for exactly what it says: **a provider was contacted and the outcome is unknown.** A draft whose write was reserved but not yet attempted is recorded as `attempting`, which says nothing about contact, so running `draft` against one is `REFUSED` — this invocation reserved nothing, contacted nothing and wrote nothing, because a claim already stood. `reconcile` is the exception and stays exit 3 on an unsettled intent: it does contact the provider to look, and its answer is about what the lookup found. Refusing it would close the one route out of `attempting`.

The intake and setup commands — `init`, `verify`, `demo`, `ingest`, `gmail-ingest` — emit a JSON document as they always have. They report no outcome because they contain no operator decision.

## Finding work and reading one opportunity

Unchanged from [operator views](operator-views.md):

```sh
careersignal opportunities --active --min-coverage 70
careersignal opportunity <id>
```

The detail view now also says where the opportunity stands as an action, not just as a record:

```
Example Company - Application Engineer
  id         6f3551ba3e49758400763a1551750f138ae3a4c5424859207aa4543e8878f803
  url        https://jobs.example.com/roles/1
  location   remote
  status     interested (event 2)
  coverage   100%
  eligible   yes
  advances   yes
  approval   approved by synthetic-operator
  draft      created; receipt controlled-282c3d5d302dc886…
  reasons
    Stated skill coverage: 2/2 (100%)
    Matched: python, sql
    Location eligible
    Advances
  wording
    I would like to learn more about Application Engineer at Example Company.
  history
    1  1789021264  new  intake
    2  1789021272  interested  operator
      Sent CV
```

Each history row is the event id, when it was recorded, the status and the actor, with the operator's note on the lines beneath it. The event id is what `--expect` names.

`approval` is `not yet decided`, `approved by <actor>` or `rejected by <actor>`. `draft` is one of:

| Shown | Means |
|---|---|
| `not attempted` | No draft has been proposed. |
| `refused; nothing was created` | A draft was refused before anything left this machine. The approval still stands. |
| `attempting; no outcome recorded yet` | A write was reserved and has not settled. |
| `uncertain; reconciliation required` | The provider was contacted and the outcome is unknown. |
| `created; receipt <id>` | The draft exists and this is its receipt. |

`refused` means the **most recent** thing that happened was a refusal, not that one ever happened. A refusal and a decision are both audit events, and audit is history: a refused draft leaves its row behind permanently, while the decision row is replaced when the operator approves again. So which of the two is current is decided by their order. Correct the recruiter's address, reapprove, and the state reads `not attempted` again — the refusal stays in the audit trail, but it has been answered. Refuse again after that approval and it reads `refused` again.

An intent is outside that ordering entirely. Once an external write has been attempted, its outcome is a fact about that attempt, and no later event revises it: `attempting`, `uncertain` and `confirmed` always win over any refusal, whenever it was recorded.

This is **reporting, not prediction**. The write paths decide for themselves whether an action is still authorized — the claim transaction rechecks every binding — so an `approved` here means an approval was recorded, not that drafting will be permitted now. A review that changed since it was approved still refuses at the moment of writing, which is the only moment where the answer cannot go stale.

The `wording` block is the draft text an approval binds to. It is labelled separately from the `draft` line above it, which is about whether one was attempted.

Every value in this view that came from outside — a title, a recruiter's subject, an operator's actor and reason, a provider's receipt — is escaped for the terminal exactly as [operator views](operator-views.md) describes. The `--json` form keeps the underlying value.

## Recording a status

```sh
careersignal status <opportunity-id> --to applied --expect 12 --actor operator --reason "Sent CV"
```

`--expect` is the status event the operator decided against, read from the detail view. It is **required**: the interface has no path that appends blindly. See [compare and append](status-history.md#compare-and-append) for why.

```
ACCEPTED  applied recorded as event 13
```

```
REFUSED   Opportunity status changed since it was read: expected event 12, found event 13 (withdrawn)
          Read the opportunity again and record against its current state.
```

A refusal writes nothing at all. The ledger is append-only and a refusal is not an event; it is the decision not to write one.

Recording a status is not an outward action and builds no provider. It creates no approval and no draft intent — see [what status is not](status-history.md#what-status-is-not).

## Approving and drafting

```sh
careersignal approve <review-id> --actor operator
careersignal draft <review-id>                                   # controlled, on this machine
careersignal draft <review-id> --provider gmail --mailbox operator@example.com
careersignal reconcile <review-id>
```

These call the existing machinery described in [approved drafts](approved-drafts.md), unchanged. The controlled provider remains the default and Gmail must be named explicitly, with its own credential in the environment.

An approval the decision rules refuse — an ineligible review, a review that predates stated skill coverage, an opportunity already ended, a review whose draft was already attempted — is `REFUSED` with the reason, not a usage error. The operator can act on what it names. A review id that does not exist at all is exit 2 instead, as above.

## What this interface is not

It is a way to operate what CareerSignal already does. It adds no capability:

- **No new state.** The status vocabulary, the decision record and the draft intent states are exactly the ones that existed before.
- **No second route outward.** The commands call `Workflow.draft` and `Workflow.reconcile`; they never claim, finish, or call a provider themselves. A test asserts this against the source, because a happy-path test would not notice a second route being added beside the first.
- **No web interface, no TUI, no colour, no interactive display, no background work, no notifications.** Output is plain text a terminal, a pipe or a file can hold.
- **No editing.** There is no command to change a status event, a decision or a receipt. Corrections are new events.

## Limitations

- **`--expect` protects the operator's own write, not a whole session.** It says the opportunity has not moved since the value was read. It does not reserve anything between reading and writing, and nothing stops a second operator from winning that race — it only stops them from doing so silently.
- **No batch operations.** One opportunity, one command. Recording the same status across many opportunities is a shell loop, and each one carries its own expectation.
- **Exit codes are the machine-readable outcome for scripts that do not parse `--json`.** They distinguish accepted, refused, uncertain and misuse, and nothing finer.
- **Runtime data is personal.** Company, title, URL, status reasons and actors come from a real mailbox and a real operator. Keep the database private; public fixtures are synthetic.
