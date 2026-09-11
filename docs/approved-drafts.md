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

Migrations 0005 and 0006 bind an approval to what was read *and to who it was for*:

```
content_digest     the review's digest of the opportunity it describes
draft_digest       the exact draft wording that was approved
status_event_id    the status history event current at the moment of approval
addressing_digest  the sender and subject the draft would be addressed with
source_message_id  which message said so (provenance, not authorization)
```

An approval authorizes an outward action, and an outward action has a target. Binding the
body without binding the recipient leaves the target free to move: an opportunity can
arrive in more than one message, replay reuses the review when the job has not changed, and
the newest source addresses the draft. So a second message about the same job, from a
different sender, moved the target of an already approved draft while every bound value
stayed identical:

```
approve while the source is jane@example.com
a later message about the same job arrives from bob@example.com
content_digest, draft_digest and status all unchanged
draft created, addressed to bob@example.com, with no second approval
```

`source_message_id` is deliberately outside the digest. The same sender and subject arriving
in a second message is not a materially different outward action, and folding the id in
would make the guard fire on ordinary duplicate alerts. It is recorded for provenance, so a
later disagreement can be traced to a specific message.

At draft time, inside the same transaction that reserves the write, all of it is checked
again. The draft is refused if:

* the approval carries no binding — rows written before this migration. They are **not**
  backfilled, because nothing records what they were for and inventing a binding would be
  a fabrication. The operator approves again, having read what is there now.
* the review's content digest changed. Absolute: the operator approved a specific set of
  words about a specific opportunity.
* the approved draft wording changed. Same reason.
* the current status is `rejected`, `withdrawn` or `closed`.
* the addressing changed — a newer source with a different sender or subject. The message
  is *"Addressing changed since approval; review and approve again."*

An ordinary move through the pipeline — `interested` to `applied`, `reviewing` to
`interested` — does **not** invalidate an approval. Those transitions do not mean the
approved words became wrong. Only ending the opportunity withdraws the authority to act
outward on its behalf.

## Provider identity

One review still permits at most one external draft attempt, and everything above bound
that attempt to a review's content, wording and address. It did not bind *where* the
attempt goes. Approve a review while composing to `controlled`, then ask to draft it again
against a real Gmail mailbox, and nothing above would have noticed: the review, the
wording, the recipient and the status were all still exactly what was approved.

Two columns, added by migration 0007, close it:

```
provider            which implementation owns the external action ("controlled", "gmail")
provider_namespace  which destination owns it ("controlled", or "gmail:alice@example.com")
```

Deliberately not a uniqueness key. `draft_intents` stays globally one-attempt-per-review;
these columns describe that one attempt, they do not open a second slot per provider or per
mailbox. A review approved for one destination and then requested against another is a
mismatch to refuse, never grounds for a second attempt.

**An approval binds a destination, not only a target.** `approve` may name one:

```sh
uv run careersignal approve <review-id> --actor operator \
  --provider gmail --mailbox alice@example.com --db var/private.db
```

Naming a mailbox at approval time needs no credential -- it records what a later attempt
will be judged against, the same way the addressing digest records a recipient before any
draft is composed. `controlled` stays the default when `--provider` is omitted, exactly as
before.

**The declared mailbox is not proof.** `--mailbox alice@example.com` is what the operator
typed, not evidence of what the compose credential actually reaches. Google documents
`GET users/me/profile` as readable under the existing `gmail.compose` grant, no additional
scope, so one read turns the declaration into a fact:

```
operator says alice@example.com
     │
     ▼
GET users/me/profile, same compose credential
     │
     ├─ emailAddress == alice@example.com  →  identity verified
     └─ anything else                      →  REFUSE, no intent, no draft POST
```

The profile read is allowlisted by its own `profile_url()`, never by `writable_url()`.
Widening the drafts allowlist to admit `/profile` would let one boundary quietly answer for
two different guarantees; `users/me/profile` stays refused there, exactly as every other
non-drafts path is.

**Where each check sits, and what each one costs:**

```
draft(review, requested provider)
  │
  ├─ existing intent?
  │    ├─ requested does not match the intent's own provider/namespace
  │    │     → REFUSE, no new write, no request made
  │    └─ matches → confirmed replays the receipt; attempting/uncertain use the
  │                 existing recovery rules -- neither re-verifies identity
  │
  └─ no existing intent
       ├─ an approval exists and names a different provider/namespace
       │     → REFUSE, no request made -- this comparison is two declared
       │       strings and costs nothing to make
       ├─ local composition preflight (unchanged; still contacts nothing)
       ├─ provider.identity() -- the one read that is not a draft operation
       │    └─ verified identity does not match the approval
       │          → REFUSE, no intent -- the profile read already happened
       └─ claim() -- atomically re-checks content, wording, addressing and now
            provider/namespace together, persists the verified identity onto
            the intent, and only then is create() ever called
```

`identity()` runs every time this point is reached, even against a review with no approval
at all yet: claim() will refuse that case regardless ("Explicit approval is required"), and
verifying first closes a narrow race an unverified declaration would leave open -- an
approval landing in the instant between the read above and the transaction is still checked
against a genuinely verified identity, not one taken on faith. For `controlled`, this is a
constant with nothing to contact; the identity check costs nothing over what was already
true before this work.

**Reconciliation is bound too, and a confirmed receipt is the only part that skips a live
check.** An uncertain or attempting intent belongs to whichever destination `claim()`
recorded onto it, and reconciling against a different one is refused before any lookup --
first on the declared comparison, which costs nothing, and then again on a live one, which
is the only way to know the declared mailbox and the credential agree:

```
reconcile(review, requested provider)
  │
  ├─ requested does not match the intent's own provider/namespace
  │     → REFUSE, zero draft lookup, zero draft create, zero requests
  └─ matches
       ├─ confirmed → return the recorded receipt; nothing to verify, nothing to search
       └─ attempting/uncertain
            ├─ provider.identity() -- the same live read draft() makes before claim()
            └─ verified identity does not match the intent's own provider/namespace
                  → REFUSE, zero draft lookup, zero draft create
            → matches → the existing lookup() proceeds unchanged
```

The declared check alone is not enough: a credential that verifies as `bob@example.com`
while the operator typed `--mailbox alice@example.com` would otherwise have `lookup()`
search Bob's drafts for Alice's intent header, silently. A confirmed intent needs neither
check to matter twice over -- it already passed the declared comparison above, and it does
no searching at all, so there is nothing left for a live read to protect. Nothing about this
work permits `create()` during reconciliation.

**Legacy rows fail closed, exactly like every binding before this one.** A decision or an
intent written before migration 0007 carries `''` in both new columns, which no real
provider name or namespace equals. An old approval no longer authorizes an outward draft --
the operator re-approves, naming a destination under the current contract. An old intent
never authorizes another write, and nothing here guesses which provider or mailbox it was
originally for from the receipt string or from today's CLI flags: that would manufacture
provenance CareerSignal never recorded.

**Wording note.** A refusal here can still follow a read: proving whose mailbox a
credential belongs to is a GET, made solely to verify identity before anything is claimed
or looked up. "REFUSE means nothing left this machine" is now slightly too strong for this
one path. What stays true has to be stated **per invocation, not per review**: REFUSE means
this invocation wrote no draft and reserved no new durable intent. A profile read is not a
draft write. That is absolute for a fresh `draft()`, where nothing was ever reserved. It is
not the same claim for `reconcile()` against an already-unsettled intent -- there, a durable
intent plainly does exist, reserved by an earlier attempt, and REFUSE says only that this
reconciliation attempt did not touch it.

This applies to the read itself failing, not only to what it finds. A 401, a malformed
profile response, or a transport failure from `identity()` is caught in both `draft()` and
`reconcile()` and reported as a refusal, never as an uncaught fault and never as
`UNCERTAIN`: nothing was reserved *by this attempt*, so there is nothing ambiguous about
the write to report. In `draft()` this carries the same atomic guard as the local
composition refusal, since it too runs before `claim()` and there truly is no intent yet.
In `reconcile()` the intent already exists and is untouched either way, so the CLI reports
the outcome as a refusal while still showing the intent's actual, unaffected state rather
than claiming there is none to show.

## Concurrency

The authorization check and the row that reserves the write are one `BEGIN IMMEDIATE`
transaction, so no status event can be appended between deciding that the opportunity is
still active and claiming the draft intent. The event id observed by that check is recorded
on the intent, and so is the source message.

**The claim returns the material it verified, and that is what is sent.** Re-reading the
address afterwards would reopen the window the check closes: a message arriving between the
claim committing and the request being made could redirect an already authorized write, or
introduce a hostile header past the point where a refusal can still be classified
correctly. There is nothing to re-read, so neither can happen.

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

**A settled intent decides the outcome before anything else is considered.** Once an
external attempt has been made, its result is a fact about that attempt, and data arriving
afterwards cannot revise it. A later message carrying a hostile address must not turn a
confirmed receipt, or an uncertain result awaiting reconciliation, into a refusal about a
draft that was never proposed. So the durable intent is read first:

```
draft()
  ↓
existing intent?
  ├─ confirmed → return the receipt
  └─ uncertain → return None; reconciliation only
  ↓ none
load current material → preflight → claim/verify/bind → create the claimed material
```

That read is for replay, and it is not sufficient on its own — an intent can appear after
it. Both paths out of it therefore carry their own atomic check:

* the **allowed** path reaches `claim()`, whose transaction refuses if an intent now exists;
* the **refusal** path never reaches `claim()`, so `refuse()` does the same job. Inside its
  transaction it returns the settled state instead of recording anything, and the workflow
  replays confirmed or uncertain from that outcome.

Without the second, a later hostile source could revise a result that an external attempt
had already made authoritative: the refusal recording would find the new intent and raise,
and the caller would lose a receipt it was entitled to.

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

As the Provider identity section above describes, one read can land before `claim()`:
verifying whose mailbox a Gmail credential belongs to. It is not a draft write and reserves
nothing, so it does not change this diagram's guarantee -- it only means "the network
starts here" is no longer literally the first request this path can make, only the first
one that writes anything.

The distinction is load-bearing rather than tidy. An intent means an external write was
attempted and its outcome may be unknown; it locks the decision for reconciliation, and
reconciliation cannot find a draft that was never created. Recording a refusal that way
would strand the review: the operator would hold an approval they cannot use, with no route
back except editing the database. Everything past `create()` keeps the uncertain behaviour
exactly as before, because past that point the outcome genuinely is unknown.

`refusal()` runs the real composition and reports what it raised, rather than checking the
same rules a second time. Two implementations of one rule are two sources of truth, and
they drift the first time only one is corrected.

## Provider rejection vs. an uncertain write

`create()` can fail in three ways, and only two of them were distinguished before this
work. A local refusal (above) never crosses the write boundary at all. Past that boundary,
most failures are genuinely unknown -- a timeout, a connection reset, a malformed success
body all leave open the possibility that Gmail created the draft anyway, so they are
recorded as `uncertain` and left for reconciliation. But a narrow set of Gmail's own
responses to the create request *prove*, rather than merely suggest, that nothing was
created:

```
create()
  │
  ├─ never contacted anything           → DraftRefused          (certain: nothing sent)
  ├─ contacted; response proves no draft → ProviderRejected      (certain: nothing created)
  └─ contacted; outcome unknown          → ordinary exception    (uncertain: reconcile)
```

**The narrowest provable set.** `REJECTED_CREATE_STATUSES = {400, 401, 403, 429}` in
`gmail_draft.py`. Each is a check Google's API gateway performs *before* a request is ever
routed to the service that would create a draft -- malformed request, rejected token,
insufficient grant, exhausted quota -- which is what makes the response provable rather
than merely plausible. A 5xx is deliberately excluded: the service may have accepted the
request and then failed while creating it, so a response existing is not proof nothing did.
An unrecognized status is excluded for the same reason -- this adapter never guesses what an
unfamiliar code means. Fewer proven rejections is always the safe direction to be wrong in.

**Retry without discarding history.** A proven rejection releases the `draft_intents` row
the attempt reserved -- `repository.reject()` deletes it, exactly as a local refusal never
reserves one -- while the rejection itself is written to the append-only audit log as
`draft_rejected`, durable evidence distinct from the row that only ever describes the
current attempt. Deleting the row rather than adding a new state to it needed no schema
change, and it means an explicit, corrected `draft` invocation can claim again immediately:

```
approve → claim → create() → 403 → reject() (row deleted, audit kept) → approval untouched
                                                                              │
operator corrects the cause, invokes draft again ──────────────────────────┘
                                                                              │
                                                                claim() re-checks content,
                                                                wording, addressing and
                                                                provider/mailbox binding --
                                                                exactly as any other claim
```

Nothing here re-derives claim()'s existing checks: they already refuse a retry whose
recipient, wording, provider or mailbox moved since the approval, the same guard that
protects every other retry path. A rejection never loosens the provider/mailbox binding
from migration 0007 -- a retry against a different destination is refused exactly as a
first attempt would be.

**Reported, not silently retried.** The CLI reports `PROVIDER_REJECTED`, sharing REFUSED's
exit code (1) and its safety -- nothing was created, and retrying once the cause is fixed
needs no reconciliation -- but under its own name, because it is a different fact: a refusal
never contacted the provider, while a rejection is what the provider itself proved once
contacted. No automatic retry exists or is planned; the operator corrects the cause (a bad
token, an insufficient grant, exhausted quota) and re-invokes `draft` explicitly.

## Correcting a source

An opportunity can arrive in more than one message, and replay reuses the review when the
job has not changed — so a review can have several sources. The **most recently ingested**
one addresses the draft.

That is a rule, not an accident. Ordering by message id would pick by hash: the recipient
of an outward draft would be chosen arbitrarily, and re-ingesting a corrected message might
or might not take effect. Row order is the arrival sequence, for the same reason status
history orders by id rather than by a timestamp.

So the recovery path is: the hostile message is refused and retained; the operator
re-ingests the opportunity from a clean message; the newest source addresses the draft —
and because the target changed, **the approval does not carry over**. The operator approves
again, having read where the message will now go:

```
REFUSE  →  correct source  →  REEVALUATE  →  target changed  →  REAPPROVE
```

That is not discarding an approval because of an error. It is recognizing that the requested
external action is now materially different. A correction that leaves the sender and subject
unchanged needs no new decision. Proven end to end rather than described.

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
mailbox is never implicit, and approving one now names the destination the same way:

```sh
uv run careersignal approve <review-id> --actor operator \
  --provider gmail --mailbox operator@example.com --db var/private.db

export CAREERSIGNAL_GMAIL_COMPOSE_TOKEN=...
uv run careersignal draft <review-id> --provider gmail \
  --mailbox operator@example.com --db var/private.db
```

The read token is not accepted in place of the compose token. A `draft` naming a different
provider or a different mailbox than the approval refuses, rather than creating a second
attempt; see Provider identity above.

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
