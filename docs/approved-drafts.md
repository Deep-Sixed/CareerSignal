# Approved provider-backed draft creation

This is the first CareerSignal capability whose failure mode is content appearing in
somebody's mailbox rather than a wrong line on a terminal. Everything below exists because
of that difference.

## Two credentials, two adapters

| | read | compose |
|---|---|---|
| module | `communications/gmail.py` | `communications/gmail_draft.py` |
| environment variable | `CAREERSIGNAL_GMAIL_TOKEN` | `CAREERSIGNAL_GMAIL_COMPOSE_TOKEN` |
| accepted scope | `gmail.readonly`, nothing else | `gmail.compose`, nothing else |
| paths | `users/me/messages`, `users/me/messages/{id}` | `users/me/drafts`, `users/me/drafts/{id}` |
| writes | none | one draft |

The read adapter is unchanged by this work. It still refuses every scope but readonly, and
`drafts` is still in its reserved-segment list, so the reader cannot address the collection
this adapter writes to. Each credential is revocable without touching the other, and a
fault in the draft writer cannot turn the mailbox reader into a write path.

## Why the scope is not the guarantee

`gmail.compose` is not a draft-only grant. Google's definition of that scope permits
creating, updating and **sending** mail. A token CareerSignal holds under it is therefore
capable of sending, and no amount of configuration changes that.

The guarantee that CareerSignal cannot send is a property of the adapter instead:

* no method sends, and none constructs a send URL;
* `DRAFT_PATH` admits only the drafts collection, so every other endpoint is refused before
  a bearer token is attached;
* `FORBIDDEN_SEGMENTS` refuses `send`, `trash`, `modify` and the rest as a final path
  segment — including `users/me/drafts/send`, which is the Gmail send endpoint and matches
  `DRAFT_PATH`'s shape exactly. That one is the reason the segment list exists.

This is asserted rather than described: a test drives the whole provider and then re-checks
every URL it actually requested against the allowlist, and each guard has been removed one
at a time with a named test confirmed to fail.

## What an approval is bound to

An approval used to record only that somebody approved, and who. That is enough to
authorize a local action and not enough to authorize writing into a mailbox: between the
operator reading a review and the draft being created, the review can be rescored, its
wording can change, or the opportunity can be ended.

Migration 0005 binds an approval to what was read:

```
content_digest    the review's digest of the opportunity it describes
draft_digest      the exact draft wording that was approved
status_event_id   the status history event current at the moment of approval
```

At draft time, inside the same transaction that reserves the write, all of it is checked
again. The draft is refused if:

* the approval carries no binding — rows written before this migration. They are **not**
  backfilled, because nothing records what they were for and inventing a binding would be
  a fabrication. The operator approves again, having read what is there now.
* the review's content digest changed. Absolute: the operator approved a specific set of
  words about a specific opportunity.
* the approved draft wording changed. Same reason.
* the current status is `rejected`, `withdrawn` or `closed`.

An ordinary move through the pipeline — `interested` to `applied`, `reviewing` to
`interested` — does **not** invalidate an approval. Those transitions do not mean the
approved words became wrong. Only ending the opportunity withdraws the authority to act
outward on its behalf.

## Concurrency

The authorization check and the row that reserves the write are one `BEGIN IMMEDIATE`
transaction, so no status event can be appended between deciding that the opportunity is
still active and claiming the draft intent. The event id observed by that check is recorded
on the intent.

This is worth being precise about, because it is not a lock on the external write. The
Gmail request happens after the transaction commits — it has to, since no write reservation
may be held across a network call. If a status is recorded during that window, the draft
was authorized against a state that was true when it was authorized, and the intent says
which state that was. That is a reconcilable record rather than a prevented race, and
preventing it is not available to any design that talks to a network.

## Refuse, correct, reevaluate

A refusal is not a discard and not an error to be forgotten:

```
receive → validate → safe   → create draft
                   → unsafe → REFUSE
                              record why
                              retain the evidence unchanged
                              operator corrects, then reevaluates
```

Concretely: a refused draft does not consume the operator's approval. Ending an opportunity
refuses the draft; recording an active status again makes the same approval usable, with no
re-approval needed. Eligibility to act is a **separate dimension** from CRM status, so the
nine-value status vocabulary is unchanged and no `quarantined` state was added.

**A refusal is not an uncertain external write, and must not be recorded as one.** The
provider is asked whether the draft can be composed *before* any durable intent is
reserved, so a refused draft leaves nothing to unwind:

```
draft_material  →  provider.refusal()  →  refused  →  audit 'draft_refused'
                                                      no intent row
                                                      approval intact
                        │
                        └─ allowed →  claim (reserves the intent)
                                   →  provider.create()   ← the network starts here
                                   →  finish(receipt) or, on any failure past this
                                      point, finish(None) = uncertain
```

The distinction is load-bearing rather than tidy. An intent means an external write was
attempted and its outcome may be unknown; it locks the decision for reconciliation, and
reconciliation cannot find a draft that was never created. Recording a refusal that way
would strand the review: the operator would hold an approval they cannot use, with no route
back except editing the database. Everything past `create()` keeps the uncertain behaviour
exactly as before, because past that point the outcome genuinely is unknown.

`refusal()` runs the real composition and reports what it raised, rather than checking the
same rules a second time. Two implementations of one rule are two sources of truth, and
they drift the first time only one is corrected.

## Correcting a source

An opportunity can arrive in more than one message, and replay reuses the review when the
job has not changed — so a review can have several sources. The **most recently ingested**
one addresses the draft.

That is a rule, not an accident. Ordering by message id would pick by hash: the recipient
of an outward draft would be chosen arbitrarily, and re-ingesting a corrected message might
or might not take effect. Row order is the arrival sequence, for the same reason status
history orders by id rather than by a timestamp.

So the recovery path is: the hostile message is refused and retained; the operator
re-ingests the opportunity from a clean message; the newest source addresses the draft; the
existing approval still stands. Proven end to end rather than described.

## Header injection

The recipient comes from the recruiter's `From` header and the subject from theirs. Both
are hostile input, and both reach message headers, where a `CR` or `LF` splits the header
and can add a reader:

```
To: jane@example.com\r\nBcc: attacker@example.com
```

Such a value is **refused, never repaired**. Sanitizing it would address the message to
somebody the operator never read. The refusal does not echo the offending value, because a
log line quoting the attempt carries the attempt.

Three findings from building this are worth recording, because each is a place where the
standard library's behaviour was nearly mistaken for a guarantee.

`email.utils.parseaddr` passes a NUL straight through into the address it returns, so
parsing is not the check; the raw value is scanned for control characters first.

**`parseaddr` does not fail closed on every supported interpreter.** On CPython 3.11.9 —
what hosted CI runs, and inside this project's supported range — it returns the *first*
address of a list rather than refusing it:

```
                                              3.11.9              3.13
'jane@example.com, attacker@example.com'  ->  'jane@example.com'  ('', '')
```

So a recruiter `From` naming two mailboxes was accepted and quietly reduced to one. That is
sanitizing a hostile value into an accepted one — the exact behaviour this boundary exists
to prevent — and it was invisible locally, because the local interpreter was newer than
CI's. Mailboxes are now *counted* with `getaddresses`, which behaves the same on every
supported version and still reads a quoted comma inside a display name as one address. The
control scan is what refused the CRLF case on 3.11.9, which is worth noting: mutation
testing had called that scan redundant on a newer interpreter.
And `compose` validates its own recipient and subject rather than trusting its caller: a
boundary that is safe only because its one current caller sanitizes first is not a
boundary. The composed message is then parsed back and checked to address exactly the
intended recipient and to carry no `Cc`, `Bcc`, `Reply-To`, `Return-Path` or `Sender`.

Stored evidence is untouched. The hostile `From` header stays in `message_sources` exactly
as it arrived, and a test asserts that — it is the premise every refusal test depends on.

## Reconciliation

Each draft carries `X-CareerSignal-Intent`, CareerSignal's own key for the attempt. If a
response is lost, reconciliation lists drafts and matches on that header rather than
guessing which draft was probably ours. Two drafts claiming one intent returns unknown, not
a guess; so does exhausting the read window without a match. An uncertain attempt is never
automatically repeated.

A 64-character intent key exceeds the header line limit and folds onto a continuation line,
which reparses with the leading whitespace folding inserted. Both the composition check and
the reconciliation comparison undo that folding. Without it, reconciliation would have
matched nothing, silently and permanently.

## Using it

```sh
uv run careersignal approve <review-id> --actor operator --db var/private.db
uv run careersignal draft <review-id> --db var/private.db
```

`draft` uses the in-memory controlled provider by default. Creating a real draft in a real
mailbox is never implicit:

```sh
export CAREERSIGNAL_GMAIL_COMPOSE_TOKEN=...
uv run careersignal draft <review-id> --provider gmail \
  --mailbox operator@example.com --db var/private.db
```

The read token is not accepted in place of the compose token.

## Limits

Public CI never contacts a mailbox: every test drives recorded synthetic payloads through
an injected transport. The compose scope is configured, not verified — CareerSignal cannot
check what a token was actually granted, which is exactly why the adapter and not the scope
carries the guarantee.

Reconciliation reads a bounded window of drafts (100 per page, 10 pages). Beyond that the
answer is unknown rather than wrong.

The draft is plain text addressed to a single recipient, with the display name dropped and
no threading headers. Replying in-thread would mean carrying more recruiter-controlled
header material, which is a separate decision.
