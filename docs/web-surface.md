# The local surface

CareerSignal's first surface that listens on a socket. It reads, it records a status, it records a decision, it runs the two outward commands — creating a draft and reconciling one — and it takes material in, from a local `.eml` or a bounded Gmail batch. It never sends: a draft is left in the destination mailbox for the operator to send by hand.

```sh
uv run careersignal serve --db /path/to/private.db
```

```
CareerSignal is reading /path/to/private.db
Open http://127.0.0.1:8765/#token=<a fresh token, printed once>
This address is valid for this process only. Stop with Ctrl-C.
```

Open that address. The URL is printed rather than opened for you: launching a browser is convenience, and this is a network boundary.

`--port` chooses a port. There is no `--host`, no `0.0.0.0` fallback, and no configuration file that could introduce one. The address is not a setting when the whole security model is "nothing leaves this machine".

## What it refuses

The surface is confined by construction rather than by configuration, so each of these is a property of the code and not of how it was started.

**It binds loopback only.** `127.0.0.1`, with no parameter that accepts another interface.

**It answers `GET`, `HEAD` and explicitly permitted `POST` command addresses.** Every other method — `PUT`, `PATCH`, `DELETE`, and verbs this server has never heard of — is refused with `405` *before* anything is routed, authenticated, or read. Refusal is still the default; `POST` narrows that default only at the explicitly listed command addresses, each written out beside the reads rather than registered in a router that would accept another the day somebody adds one. A `POST` to any other address is `405` with `Allow: GET, HEAD`, which also keeps a `POST` from reporting which read routes exist.

**It reaches read projections, and the mutations declared below.** This block is the canonical statement of what the browser may change, and it is not prose — a test parses it and compares it against what the package's own syntax tree proves it can reach:

<!-- careersignal-web-capabilities
record_status
decide
draft
reconcile
ingest_eml
ingest_gmail
-->

`record_status()` appends a status event; `decide()` records an approval or a rejection; `draft()` and `reconcile()` are the outward workflow, reached on the service that owns it; `ingest_eml()` and `ingest_gmail()` are intake, reached on the service that owns *that*. Nothing else: no `ingest()`, no `intake_message()`, and no `claim()`, `finish()`, `refuse()` or `reject()` — this surface asks `OutwardActions` and `IntakeActions` for their workflows rather than reimplementing either, and constructs no provider, no reader and no credential of its own.

The two intake entries are what PR 9 added, and what they admit is worth stating exactly. This package may now *invoke* two named intake commands on a service it was handed. It still may not name `IntakeActions`, so it cannot construct one; it still may not name `Intake`, `ingest`, `intake` or `intake_message`, so it cannot reimplement what the service does; it still may not name `Message`, `Profile`, `GmailReader` or `GmailCredentials`, so it can neither parse a message, choose an evaluation profile, nor reach a mailbox except through the service that owns that authority. It also may not call `getattr`: a dispatcher that looked a command up by name would be an authority the syntax tree cannot see, which is the one way a capability could arrive without appearing here.

The comparison runs in both directions and is an exact set equality, so a capability added to the code without being declared here fails the build, and a capability declared here that the code cannot actually reach fails it too. Order does not matter; this is a set, not a formatting convention.

What counts as a capability is **a write entrypoint this package invokes**, not a repository member it names. The two differ, and the difference is the whole point: the outward boundary is a service, so `OutwardActions.draft()` reaches `claim`, `finish`, `refuse` and `reject` without its caller naming any of them. A rule that looked only for members of a variable called `repository` would let this surface acquire the entire outward workflow while still declaring nothing — passing, and wrong. So the derivation is seeded from the definitions that open a transaction and closed over calls, and a surface that reimplements the workflow itself is caught by the same rule, because it would name those writes directly.

An entrypoint is attributed to the class that owns it, so this is not a search for a method name. `draft` is a capability when it is reached on an `OutwardActions`; an unrelated object with a method spelled the same way is not outward authority. Ownership is earned per definition rather than shared by name, and a receiver that cannot be resolved — an injected `repository`, a parameter — falls back to the name, which errs toward asking for a declaration rather than omitting one. A test proves no entrypoint name has two owners, so that fallback cannot quietly attribute a capability to the wrong thing.

Widening what the browser may do therefore means editing this block deliberately, in the same change — which is the point of keeping the declaration here, next to the explanation of what these commands are, rather than duplicating a machine-readable list into every file that mentions them.

**It derives no fact.** Every route hands back what a repository projection already returned. The rules were settled where the storage is; a second opinion computed at the edge is how a list and a detail pane start disagreeing about the same opportunity.

**It interpolates nothing into HTML.** `json.dumps` is the escaping boundary, and the page fills itself in with `textContent`. Recruiter-controlled text is never parsed as markup on the one origin that holds the launch credential.

## The launch token

Authority is a fresh high-entropy token, minted by `secrets` once per launch and held in process memory for the life of that process.

It is never written to the database, to configuration, to an environment variable, to a file, or to a log — the request logger writes nothing at all, precisely so the token cannot outlive the process in a file nobody thinks about. There is no parameter that supplies one: a token a caller could set is a token a script could pin and reuse.

It travels to the browser in the **URL fragment**, which browsers do not send in an HTTP request. It therefore reaches the page and nothing else. `api.js` moves it into session storage and rewrites the address bar immediately, so it does not survive in history, in a bookmark, or in whatever gets pasted into a chat window next. In a query string it would reach the server, the request line, and every log that copies one.

It comes back as a request header and is compared in constant time:

```
X-CareerSignal-Token: <launch token>
```

A missing or wrong token on an `/api/v1/` request is `401`. The static page itself needs no token — it has to load before it can present one — and carries no stored evidence.

The API reports whether a Gmail credential is configured. It never reports its value.

## Who may ask

A request naming an unexpected `Host` is refused with `403`. The socket is not the identity; the name the client asked for is, and a hostname an attacker owns pointed at loopback is exactly the shape DNS rebinding takes.

A request carrying an `Origin` other than the server's own is refused with `403`, and **no `Access-Control-Allow-*` header is ever emitted** — on any response, including the refusals. Another origin is not refused and then quietly handed the answer by a permissive header.

A request target must be a plain path. One naming its own authority — `http://127.0.0.1:8765/api/v1/…`, the absolute form — is refused with `400`, because HTTP makes that authority the one identifying the server while this surface settles identity on `Host`. Honouring it would mean routing by one authority and checking another, and would give every address a second spelling.

Addresses are matched whole, not by prefix arithmetic. `/api/v1` is proven to be the prefix before anything beneath it is read, so a lookalike of the same length — `/abcdef/…` — reaches nothing. Empty path components are kept rather than filtered away, so a doubled or trailing slash is a different address than the one documented here rather than an alias for it.

Every response carries:

```
Content-Security-Policy: default-src 'self'; base-uri 'none'; object-src 'none';
  frame-ancestors 'none'; form-action 'self'; connect-src 'self'; img-src 'self' data:;
  style-src 'self'; script-src 'self'
Referrer-Policy: no-referrer
X-Content-Type-Options: nosniff
Cache-Control: no-store
```

There is no `'unsafe-inline'` and no `'unsafe-eval'`, and the page ships no inline script, so the frontend that arrives next is served under this policy unchanged rather than loosening it on the way in.

## The routes

All under `/api/v1`, all `GET`, all exactly the repository projection they name.

| Route | Answers |
| --- | --- |
| `/session` | Runtime facts: SQLite version, database path, credential **presence** booleans |
| `/opportunities` | The list, with the same filters the CLI accepts |
| `/opportunities/{id}` | One opportunity with its packet, action state and history |
| `/opportunities/{id}/sources` | Every message it arrived in, oldest first |
| `/communications` | Every message, in arrival order, with its evidence counts |
| `/communications/{message}` | One message, its evidence, and what it currently addresses |
| `/reviews/{review}/authorization` | What an approval for this review would bind |
| `/timeline` | Status and audit events as one stream, newest first |
| `/intake/sources` | Which sources this launch can take material in from, and the profile it scores against |

`/opportunities` accepts `status`, `active`, `eligible`, `min_coverage` and `max_coverage`; `/timeline` accepts `limit` and `since`. `/opportunities` and `/opportunities/{id}` also accept `presentation`. The values are handed to the projection that owns them, so a coverage bound outside 0–100 or a status outside the vocabulary is refused in the repository's own words.

A parameter a route does not know is a `400`, not a silent pass: a misspelled filter that returned everything would tell the operator they are looking at a narrowed list when they are looking at all of it.

A blank value is a value. `?status=` and `?misspelled=` are both refused rather than read as "no filter" — the query is parsed with `keep_blank_values` for exactly that reason, because a parameter dropped at the parsing boundary is never validated and comes back as the whole list wearing the shape of a narrowed one.

An id that names nothing is `404`. A record that exists but has no evidence is an honest empty list — absence of a record and absence of evidence must not arrive looking the same.

## Presentation

`GET /api/v1/opportunities?presentation=true` and `GET /api/v1/opportunities/{id}?presentation=true` add one namespaced field and change nothing else:

```json
{
  "id": "…", "company": "…", "status": "interested",
  "presentation": {
    "coverage": "100%",
    "queue": "draft",
    "approval": "approved by operator",
    "attempt": "not attempted"
  }
}
```

Every one of those four strings is produced by calling `system.views.coverage()`, `queue()`, `approval()` and `attempt()` — the same functions the CLI prints through. Nothing is recomputed at the edge, and a test reads the package's syntax tree to prove that no fifth derivation, precedence table, status vocabulary or draft wording has appeared in the web layer or in the JavaScript.

Without `presentation=true` the responses are the repository projections exactly as they were before this existed, so enrichment is opt-in and the established reads are unchanged. `presentation` follows the same fail-closed rule as every other parameter: `true` and `false` are accepted, and a blank, an unknown value or a repeat is a `400`.

Building a row's presentation needs its action state, which `opportunities()` does not report, so the list reads each opportunity's detail. That is one read per row, accepted deliberately: the alternative is a second summary model kept in step with `_action` by hand, which is the duplication this design exists to avoid. It is a local, single-operator application, and it can be optimised when a real database is measurably slow.

## The screens

Five, as frozen: Dashboard, Inbox, Opportunities, Approvals, Activity — Inbox now carrying the intake controls described below. Plain HTML, CSS and ES modules — no framework, no bundler, no npm, no CDN, no downloaded font. The browser owns interaction and rendering; it does not own interpretation.

The browser may fetch, select a row, filter rows it already has, count statuses and queues, switch panes, format dates and build DOM nodes. It may not decide whether an approval is stale, whether something needs reconciling, whether a draft may be created, what `REFUSED` means, whether an opportunity advances, or which state outranks another. Those arrive already decided.

Everything stored reaches the page through `document.createElement` and `textContent`. There is no `innerHTML`, no `insertAdjacentHTML`, no `document.write`, no `eval`, and no template that concatenates a value into markup — so a recruiter's subject line has no parser to reach on the one origin that holds the launch credential. Non-printing characters are escaped for display, never removed: a title carrying an escape sequence stays visible as evidence. The one attribute taken from stored text, a link's `href`, is restricted to `http` and `https`, so a stored `javascript:` URL renders as struck-through text.

The browser may write only what the capability block above declares. **Approve**, **Reject**, **Re-approve** and **Withdraw approval** are here, in the approval packet itself; every command it may send is written out below. **Create draft** and **Reconcile** are here too, below the decision rather than beside it, because authorizing an action and performing it are different steps and should not read as one control group. Approvals is where the decision is taken: it shows the rows whose queue makes approval state relevant, with the bound packet and the approved-versus-now digests beside the controls that act on them.

## The commands

```
POST /api/v1/opportunities/{id}/status
Content-Type: application/json
X-CareerSignal-Token: <launch token>
Origin: http://127.0.0.1:<port>

{"status": "interviewing", "reason": "Recruiter scheduled technical interview", "expected_event_id": 103}
```

`status` and `expected_event_id` are required; `reason` is optional and defaults to `""`.

**There is no `actor` field.** The authenticated local browser *is* the operator, so the server records `actor="operator"`. An actor taken from the request would let presentation input rewrite audit identity. Supplying one is refused rather than ignored — silently dropping a field a caller believed in is how a surface ends up recording something other than what was asked for.

**The compare-and-append is `record_status()`'s, not this layer's.** It reads the newest event inside the write transaction and refuses there. Nothing in the HTTP layer re-implements that comparison: a second one here could only read outside the transaction, which is the race it exists to close. There is no application-level write lock either — a lock in the web server would be a second concurrency authority, and the wrong one. Two commands carrying the same event race honestly, and the database decides: one appends, one is told what it found.

**Provenance is settled before the body is read.** Host, then `Origin`, then the launch token — so a request that cannot say where it came from is never parsed. `Origin` is *required* on a command, not merely checked when present: a read with no `Origin` is an ordinary same-document fetch, but a write with none has nothing to say for itself. The body must be `application/json`, must declare a `Content-Length`, and must not exceed 16 KiB — refused on what it declares rather than after it has been read into memory.

| Outcome | Response |
| --- | --- |
| appended | `200` with the status, the new event, and the previous one |
| malformed or invalid command, or a target naming an authority | `400` |
| missing or wrong launch token | `401` |
| missing or wrong `Origin` | `403` |
| unknown opportunity | `404` |
| the event moved since it was read | `409` |
| body larger than the ceiling | `413` |
| any other method, or a `POST` elsewhere | `405` |

A conflict is not an ordinary `400`. The request was well formed and the operator's authority was real; what moved was the state they acted on. It is caught by type — never by reading an exception's text — and answers with what was found:

```json
{"error": "status_conflict", "expected_event_id": 103, "observed_event_id": 104, "status": "withdrawn"}
```

Nothing is written.

## The status vocabulary

`GET /api/v1/statuses` returns the ordered vocabulary from `recruiting.status.STATUSES`. The browser fills its control from that rather than holding a copy that could fall out of step. The order is presentation order and carries no rule: transitions are deliberately unrestricted, and only the vocabulary is governed.

## The decision command

```
POST /api/v1/reviews/{review_id}/decision
```

An approval carries the packet it was read from; a rejection carries nothing.

```json
{"approved": true,  "expected": {"content_digest": "…", "draft_digest": "…",
                                 "addressing_digest": "…", "status_event_id": 103}}
{"approved": false}
```

The asymmetry is `decide()`'s, not this layer's invention. An approval authorizes an outward draft to a destination, so it is recorded only while the four facts the operator saw still hold. A rejection binds nothing and authorizes nothing, and demanding a fresh packet to record one would put the safest action an operator can take behind the same precondition as the riskiest. So `expected` is **required** with `approved: true` and **refused** with `approved: false` — refused rather than ignored, because a caller that sent one believed it was being honoured.

**The expectation comes from the packet that was rendered.** `GET /api/v1/opportunities/{id}` returns `bound`, and `bound.expected` is built from the same `_binding()` snapshot as the recipient, subject and wording beside it. Reading the packet from one request and its expectation from another would name a moment the operator was never shown — which is the window the expectation exists to close. The browser posts back the object it was given and never assembles one.

**The comparison is `decide()`'s.** It reads the binding inside the write transaction and raises `BindingConflict` there. Nothing at this layer pre-checks it: a comparison here could only read *outside* that transaction, and a message could commit between the check and the row that depended on it.

| Condition | Response |
| --- | --- |
| decision recorded | `200` |
| malformed body or malformed expectation | `400` |
| bad/missing launch token | `401` |
| bad/missing `Origin` | `403` |
| review does not exist | `404` |
| the packet moved | `409` `binding_conflict` |
| a valid decision the current state refuses | `409` `decision_refused` |
| oversized body | `413` |
| a `POST` anywhere else | `405` |

A state refusal — a terminal opportunity, a below-threshold review, a decision locked behind an existing draft intent — is **not** a malformed request. The body was well formed and the operator's authority was real; what refused was the stored state, so it answers `409`, not `400`. Telling an operator to fix a request that was never wrong is its own kind of wrong answer.

`binding_conflict` carries what the exception already knows:

```json
{"error": "binding_conflict", "expected": {…}, "observed": {…}}
```

Nothing is written when it is raised: no decision row, no audit event. An approval that already stood stands exactly as it was.

## The outward commands

```
POST /api/v1/reviews/{review_id}/draft
POST /api/v1/reviews/{review_id}/reconcile
```

Both carry `{}`. The address is the whole of the request: what a draft would say was settled by the approval, and reconciliation asks the destination what happened rather than telling it anything. A body carrying any field at all is refused rather than ignored — `actor`, `provider` and `expected` are all things this surface decides for itself, and accepting them silently would read as though a page could choose them.

**Every rule belongs to `OutwardActions`.** Whether an approval exists, whether it still binds the packet on screen, whether the destination matches, whether an intent already stands — none of it is pre-checked here. A check at this layer could only read outside the transaction that depends on the answer, which is the race the claim exists to close. It is the same reason the decision command does not compare bindings and the status command does not compare events.

### Four outcomes, never two

A draft attempt has four possible answers and they are not degrees of success. Each says something different about whether a draft now exists in the operator's mailbox, and each calls for a different move:

| Outcome | What it means | What the operator does |
| --- | --- | --- |
| `accepted` | The provider confirmed creation, and the receipt is known | Read the draft in the mailbox and send it by hand |
| `refused` | CareerSignal refused locally; no draft-create request was ever sent | Correct the cause — the approval still stands — and try again |
| `provider_rejected` | A request was sent and the provider's own answer proves nothing was created | Correct the cause and draft again; no reconciliation is needed |
| `uncertain` | A request was sent and the outcome cannot be established | **Reconcile.** Never draft again first |

`refused` and `provider_rejected` share the property that nothing exists out there. They are reported separately because they are not the same fact: one means no request was ever sent, the other means one was sent and came back proving it failed. A provider may only report the second where its own documented contract says a response proves non-creation; anything it cannot prove that way falls to `uncertain`, because fewer proven rejections is always the safe direction to be wrong in.

`uncertain` is the one that matters. It is not a failure — it is the honest answer when a draft-create request may or may not have landed.

### After an uncertain attempt, nothing creates a second draft

This is the property the surface is built around.

An unresolved attempt leaves a durable intent, and `draft()` stops on that intent before it composes anything. Asking again writes nothing and contacts nothing; it reports the same unresolved state and points at reconciliation. There is no retry, no timer, no automatic repeat, and no **Draft again** control anywhere in the browser — the absence is deliberate, and a test asserts it against the shipped file.

**Reconciliation never creates a draft.** Its authority over the provider is read-only: it searches the destination for this review's own intent key and records what it finds.

- **Found** — the existing intent is confirmed with its receipt. Nothing is created.
- **Not found** — the attempt stays `uncertain`. Unknown is not evidence of absence: a search that found nothing has established that *this search* did not find it, and turning that into permission to draft again is the single most dangerous thing this surface could do.
- **Cannot verify the destination** — `refused`, and the existing intent is untouched. A lookup made with a credential that cannot be proven would search a mailbox the attempt was never made against and report its silence as evidence.

**A confirmed attempt is idempotent.** Drafting or reconciling again returns the receipt that already exists and contacts the provider for nothing new.

### Two operators, one review

The reservation is the database's to grant, not the web server's. Two simultaneous drafts produce exactly one provider draft: the claim is a write the database serialises, and the losing request is told the attempt is already under way rather than being given one to make. There is no in-process lock — a mutex in the handler would hold for one process, and the repository is the source of truth.

| Condition | Response |
| --- | --- |
| the attempt ran — `accepted`, `provider_rejected` or `uncertain` | `200` with the record |
| CareerSignal refused locally — `refused` | `409` with the record |
| malformed body, any field, or an unknown parameter | `400` |
| bad/missing launch token | `401` |
| bad/missing `Origin` | `403` |
| review does not exist | `404` |
| oversized body | `413` |
| `GET` or `HEAD` on either address | `404` |
| any other method | `405` |

Reading one of these addresses is `404` rather than `405`: the read router has no such route, and these two answer `POST` only. A method this surface does not answer at all is still refused with `405` before anything is routed.

`refused` answers `409` because it is a conflict with what is stored — a stale approval, a destination that is not the approved one, an intent already standing — exactly as a refused decision is. The other three ran, and they are answers rather than errors.

### Outward authority is optional at launch

Recording a decision has never needed a credential that can reach the mailbox; creating a draft does. So a Gmail launch without a compose token serves decisions and refuses to act on them, rather than refusing to start — making the safe half of the workflow depend on the unsafe half is the dependency that was deliberately removed. `/session` reports `outward` as a boolean, never a credential, and the browser says so plainly instead of offering a control that could only ever refuse.

**CareerSignal never sends.** `draft` creates a draft and stops. The operator reads it in the mailbox and sends it themselves.

## Intake

Two commands take material in, and one read says which of them this launch can actually run.

```
GET  /api/v1/intake/sources
POST /api/v1/intake/eml
POST /api/v1/intake/gmail
```

There is deliberately no `POST /api/v1/intake` that takes a `source` field. Each authority is named by its own address, for the same reason the outward pair is: an address a request could choose is an authority a page could choose, and a generic dispatcher would put the choice of *which provider to reach* inside a body that a page assembles.

**This is a web exposure of intake, not a second intake.** Everything that arrives goes through `Message` → `Intake.intake_message()` → `Repository.ingest()`, the same path `careersignal ingest` and `careersignal gmail-ingest` have always used, now shared as one `IntakeActions` service. There is no second MIME parser, no second extractor, no second scorer, no second deduplication scheme and no browser-side notion of what a job is. The browser acquires the ability to ask intake to run. It does not acquire the authority to define what intake means.

### The profile is a launch fact

Which skills and which accepted locations an opportunity is judged against is configuration, supplied at launch by the same two options the pipeline commands take:

```sh
careersignal serve --skill SailPoint --skill IAM --location remote
```

`--skill` is what gives a launch intake authority at all. Without one there is no profile to score against, so `/intake/sources` reports intake unavailable, the browser offers no intake control, and both commands answer `409`. Locations default to `remote`.

**No intake request may carry `skills`, `locations`, `profile`, `coverage`, `eligibility` or `score`** — there is no request shape that has a place for one, and the Gmail body refuses every field it does not name. This is not tidiness. The material being scored is written by a recruiter, and material that could choose the policy it is judged against is not material being judged.

### Read authority is not draft authority

Gmail intake needs `CAREERSIGNAL_GMAIL_TOKEN`, the read-only grant, plus `--mailbox`. Creating a draft needs `CAREERSIGNAL_GMAIL_COMPOSE_TOKEN`, a different grant, described in [approved drafts](approved-drafts.md). Neither is a flag; both are environment-only, because a command line is visible in shell history and to other users of the machine. They are separate credentials and separately optional, so a launch may legitimately be

```
EML intake: yes    Gmail intake: yes    outward drafting: no
EML intake: yes    Gmail intake: no     outward drafting: yes
```

and the browser is told which, so it offers only controls that can operate.

`system.web` constructs neither credential and imports neither adapter. Both services arrive already built from the composition root that parsed the command line.

### The mailbox is proven, not declared

`--mailbox` is what the operator typed. Before a single Gmail message is admitted, `GmailReader.verify_identity()` reads the credential's own profile and compares the verified namespace against the declared one. A mismatch fails closed, before the first local write, and no message from that credential is ever stored under a namespace it does not answer to. The browser cannot supply a mailbox and cannot confirm one; a page saying "yes, that is the right mailbox" would be a declaration replacing the proof.

### `GET /api/v1/intake/sources`

Discovery of **CareerSignal's** configuration, not of a mailbox. It contacts nothing: a page load must not spend a credential, and a read of local configuration must not depend on whether Google is reachable.

```json
{
  "eml": {"available": true},
  "gmail": {"available": true, "namespace": "gmail:user@example.com"},
  "profile": {"skills": ["iam", "sailpoint"], "locations": ["remote"]}
}
```

With no launch profile:

```json
{"eml": {"available": false}, "gmail": {"available": false}, "profile": null}
```

`namespace` appears only where there is a reader to name one. No token, no token length, no fragment of a token and no credential scope detail appears here, and `gmail.available` means *configured for this launch* — not that Google has just been asked.

### `POST /api/v1/intake/eml`

The body **is** the message.

```
POST /api/v1/intake/eml
Content-Type: message/rfc822
X-CareerSignal-Namespace: <operator-declared source>
X-CareerSignal-Token: <launch token>
Origin: http://127.0.0.1:<port>
Content-Length: …
```

Nothing JSON-wraps or base64-wraps an email to fit a body helper that already exists: a MIME message has a representation, and re-encoding one would mean this surface had an opinion about its bytes.

**The server never accepts a filesystem path.** The browser reads the file the operator chose and sends what it read, so there is no name for the server to resolve and no directory it could be pointed at. The filename is a local convenience in the page and is never stored as provenance.

**The namespace is operator-declared provenance and is not verified.** It is deliberately not derived from the filename, the sender, the `Message-ID`, the subject or the body — every one of those is written by whoever sent the mail, and a message that named its own source would choose which mailbox CareerSignal believes it arrived in. The Inbox labels it as declared. Exactly one such header is accepted; a blank one is refused rather than defaulted, because an empty namespace is the operator not having said, and CareerSignal has no answer to invent.

The ceiling is `MAX_MESSAGE_BYTES`, the same one `Message` itself enforces — imported, not restated, because a second number could only ever disagree with the one that counts. A declared length above it is `413` before the body is read, and exactly the declared number of bytes is read and never one more.

A success is the record of what happened:

```json
{
  "source": "eml",
  "namespace": "…",
  "message": "message:…",
  "external_id": "…",
  "reviews": ["…"],
  "diagnostics": [{"item": 0, "reason": "no job URL", "review": null}]
}
```

**A valid message that names no opportunity is a successful intake.** Extraction diagnostics are data, not an HTTP failure: what the extractor did not understand is exactly what the operator needs to see, and answering `400` would tell them to fix a request that was right.

### `POST /api/v1/intake/gmail`

```json
{"query": "from:recruiting", "labels": ["INBOX"], "limit": 25}
```

All three fields are optional and default to `""`, `[]` and `25`. Every other field is **refused**, `mailbox`, `token`, `namespace`, `skills`, `locations`, `provider` and `profile` among them: the launch owns those facts, and quietly dropping a field a caller believed in is how a surface ends up reading a different mailbox than the request asked for.

`limit` is a whole number in `1…100` — a boolean is not an integer, and `true` is not a number of messages. `labels` must be an array of strings; a bare `"INBOX"` is refused rather than spelled out one letter per label. The body keeps the ordinary 16 KiB command ceiling. No Gmail search can turn one button press into an unbounded mailbox walk.

**The whole batch is read before the first local write.**

```
validate the command
    ↓
verify the mailbox identity
    ↓
read the complete bounded batch
    ↓
only then: intake, message by message
```

`GmailReader.messages()` already returns the entire batch or raises, and that is why it is called rather than rewritten as fetch-one-write-one. If message N cannot be read, messages 1…N−1 from that request are not already stored merely because they were fetched first, and no write reservation is ever held open across a network call.

```json
{
  "source": "gmail",
  "mailbox": "gmail:user@example.com",
  "read": 3,
  "messages": [{"external_id": "…", "message": "message:…", "reviews": ["…"], "diagnostics": []}]
}
```

The namespace is the **verified** one, from the reader. The browser does not supply it.

### Idempotency is the storage's, not the browser's

There is no second deduplication layer. Message identity belongs to `Message` and `Repository.ingest()`, and a repeat import resolves to the records that already exist — the same Gmail message id imported twice does not become two communications because a button was pressed twice, and the browser never manufactures an intake id that would defeat storage identity. Where the repository reports an already-known message, that existing state is returned honestly rather than dressed up as new.

### What new evidence does to what is already there

New material can legitimately change what a later action sees: a further source can produce a new current review, or make an earlier approval stale. That belongs to `Intake`, `Repository.ingest()` and the repository's projections. This layer does not invalidate approvals, choose which review is current, merge opportunities, score skills, calculate eligibility or decide that newer evidence supersedes older. After intake it re-reads and shows what the authoritative layer now says.

| Condition | Response |
| --- | --- |
| taken in — including zero reviews with diagnostics, and a replay resolving to existing records | `200` |
| malformed command, wrong field type, unknown field, bad `limit`, blank or missing namespace, unparseable MIME | `400` |
| bad/missing launch token | `401` |
| bad/missing `Origin` | `403` |
| no launch profile, or Gmail requested without a read credential | `409` `intake_unavailable` |
| body larger than the ceiling | `413` |
| the EML route sent anything but `message/rfc822` | `415` |
| the mailbox could not be read | `502` `source_unreadable` |
| `GET` or `HEAD` on either command address | `404` |
| any other method | `405` |

`409` is a launch-capability conflict, not evidence that Gmail rejected anything: nothing external was read and nothing local was written.

`502` is an upstream read failure after the command was accepted — a wrong or expired token, a rate limit, an identity that is not the declared one. The message is the adapter's own and never contains the bearer token, an `Authorization` header, a response body or a traceback. Because the batch is read before intake begins, **no message from that attempted batch was written.**

### Recruiter-controlled content stays inert

Nothing about intake weakens the browser-content boundary. An uploaded or fetched message may carry HTML, scripts, CSS, control characters and hostile sender names; the server normalises it through `Message`, and the browser receives repository projections and diagnostics only. There is no `innerHTML`, no `iframe srcdoc`, no HTML preview and no raw-message preview. The intake UI is not an email client.

### The storage model is unchanged

Uploading a file expands no persistence. No table holds a full raw RFC822 payload, no attachment is extracted or stored, no browser-local path is kept, and no credential or launch token is written anywhere. What is stored is exactly the normalised source, evidence and review information the existing intake path has always stored.

### The Inbox controls

Inbox gains one clearly separated intake section, offered only where it can operate — `/intake/sources` decides that, not an inference from unrelated facts such as whether outward drafting is configured.

**Import EML** takes a source namespace and a local file. The browser reads the chosen file as bytes and sends them unchanged as `message/rfc822`; it does not parse the message and does not inspect recruiter content to decide whether it is a job.

**Import Gmail** takes a query, optional label ids and a bounded limit. When Gmail intake is unavailable the control is absent rather than present and failing: a button that can only refuse teaches the operator to ignore refusals.

Afterwards the browser re-reads — Inbox, Opportunities and Approvals alike — rather than patching local objects into pretending the new state. It reports how many messages were read, the provenance already exposed safely, the review ids created or resolved, and the extraction diagnostics, all through the same `textContent` construction every other value uses.

**Nothing here sends, modifies, labels, deletes or polls.** There is no Gmail watch, no push subscription, no mailbox polling, no folder watching and no scheduled intake. Intake happens when the operator presses the button.

## Where an approval points

The destination is a **launch fact**, declared the way the command line already declares one:

```
careersignal serve --provider controlled
careersignal serve --provider gmail --mailbox user@example.com
```

`--provider gmail` derives the namespace with `namespace_for(mailbox)`, the same pure function the compose side uses, so a declared mailbox and a mailbox Gmail itself reports become the same string by the same code. **No compose credential is required.** Approving names where a draft may go; it has never needed the ability to reach it, and making the safe half of the workflow depend on the unsafe half would be the wrong dependency. Only the two resulting strings cross into `system.web` — no adapter, no credential class.

`GET /api/v1/session` reports the target as a name, never a secret:

```json
{"decision_target": {"provider": "gmail", "provider_namespace": "gmail:user@example.com"}}
```

It is immutable for the process. A restart issues a new launch token anyway, so a page left open cannot act against a target chosen after it loaded.

An approval may bind one destination while this launch is configured for another. The packet says so, side by side, and offers to re-approve against the current one — but only where the queue allows a decision at all. Once a draft has been attempted the decision is locked, and copy promising a re-approval that cannot happen would be a button that lies. This comparison is presentation: the draft path rechecks the destination transactionally, and a screen is never authorization.

## Approval controls stay on the packet

There is no toolbar of verbs. The controls live in the packet block, and the expectation they carry is `bound.expected` — the same object whose recipient and wording are rendered above them. A button hovering over the current selection would have no particular packet behind it, and an approval that cannot name what it was decided against is exactly what `decide(expected=…)` exists to prevent.

Which controls appear is looked up by the queue the server already decided, from a flat table keyed by queue name. The browser does not work out whether an approval still binds: `views.queue()` owns that precedence, it is mutation-tested where it lives, and a second implementation here is how a list and a detail pane start disagreeing. A queue the table does not name offers nothing.

**Withdraw approval** is wording for recording `approved: false`. It creates no new state, audit vocabulary or schema — afterwards the durable decision is the ordinary negative one. Reversing that is a command-line action in this release.

## What the browser does after a command

It re-reads. Nothing patches the row, the status, the queue or a count locally — what those become is the engine's to decide, and a local edit would be the browser forming a second opinion about state it just asked the server to change.

A refusal is re-read too, and the form stays open on the new state, showing what the opportunity is *now* and inviting the operator to decide again. The command is never retried against the newer event: deciding again is theirs to do.

## Static assets

`index.html`, `careersignal.css`, `icon.svg` and four ES modules (`app.js`, `api.js`, `dom.js`, `screens.js`) are packaged resources under `system.web`, discovered with `importlib.resources` and served by **exact name**. Nothing joins or resolves a path, so a request for `../../etc/passwd` is not a traversal to defeat — it is a key that does not exist. There is no directory listing. An asset the surface cannot name a media type for stops the launch rather than being served as guessed bytes.

Locating assets beside `__file__` would pass every test in a checkout, so the wheel gate starts the surface from the installed wheel with no checkout anywhere and asks it for them.

## Threads and connections

The server is threaded so a slow read cannot block the pane beside it, and no `sqlite3` object crosses those threads. `Repository` holds a path; each of its methods opens and closes its own connection on the calling thread. That is why one handle can be shared where a connection could not be, and why this package creates no pool and never weakens `check_same_thread`.

## What is not here yet

The repository's own writes. `claim()`, `finish()`, `refuse()`, `reject()`, `ingest()` and `intake_message()` are all unreachable from this package, and a test fails the build if that changes — the browser reaches the two workflows by asking the services that own them, never by performing what those services perform. (A previous edition of this section still said drafting and reconciling themselves were unreachable; they became reachable through `OutwardActions` in PR 8, and the capability block above has been the authoritative statement throughout.)

The persisted evaluation profile. `/intake/sources` now reports the skills and locations *this launch* was started with, which is a fact about the process rather than about the database: the engine still stores no profile, and the threshold is not reported because it is not configurable at launch. A profile that survives a restart arrives with the persisted-profile decision.

Gmail label discovery. The Gmail command accepts label ids the operator knows; it cannot list the mailbox's labels, and `labels` is in the read allowlist's reserved-segment list precisely so nothing here can reach that collection.

Scheduled or background intake. No mailbox polling, no Gmail watch, no push subscription, no folder or directory watching. Intake happens when the operator presses the button, and a surface that imported on its own would be making a network request nobody asked for.

Attachments, and previewing a message before taking it in. Nothing extracts or downloads an attachment, and nothing renders a message body — the intake UI is not an email client, and a preview would be the one place recruiter HTML had a parser to reach.

Sending. CareerSignal stops at draft creation and always has. The draft is left in the destination mailbox and the operator sends it themselves, from the mailbox, after reading it. No surface here has ever had a send path and none is planned; a machine that could both decide to write to someone and then send it is a different product.

Reversing a decision to decline. A rejected review can be approved again from the command line; this surface reports the decision and offers no control for it. A first write surface should not also be the place that quietly re-opens something an operator deliberately closed.

A readable database path. `/session` reports the path in full and the panel prints it in full, so a long one is the widest thing on the screen and pushes the layout around. The fix is to shorten it **in the middle**, keeping the start and the filename, since those are the parts that identify which database is open — and to keep the whole value reachable, as a title attribute and as selectable text, because a path the operator cannot read back is a fact the panel only appears to report. The truncation is presentation alone: the projection keeps serving the real value, and nothing downstream reads the shortened form.
