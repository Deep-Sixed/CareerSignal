# The local surface

CareerSignal's first surface that listens on a socket. It reads, it records a status, and it records a decision. It creates no draft and contacts no mailbox: drafting and reconciling are still command-line actions.

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
-->

`record_status()` appends a status event; `decide()` records an approval or a rejection. Nothing else: no intake, no `claim()`, no `finish()`, no `draft()`, no `reconcile()`, no provider, no credential.

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

Five, as frozen: Dashboard, Inbox, Opportunities, Approvals, Activity. Plain HTML, CSS and ES modules — no framework, no bundler, no npm, no CDN, no downloaded font. The browser owns interaction and rendering; it does not own interpretation.

The browser may fetch, select a row, filter rows it already has, count statuses and queues, switch panes, format dates and build DOM nodes. It may not decide whether an approval is stale, whether something needs reconciling, whether a draft may be created, what `REFUSED` means, whether an opportunity advances, or which state outranks another. Those arrive already decided.

Everything stored reaches the page through `document.createElement` and `textContent`. There is no `innerHTML`, no `insertAdjacentHTML`, no `document.write`, no `eval`, and no template that concatenates a value into markup — so a recruiter's subject line has no parser to reach on the one origin that holds the launch credential. Non-printing characters are escaped for display, never removed: a title carrying an escape sequence stays visible as evidence. The one attribute taken from stored text, a link's `href`, is restricted to `http` and `https`, so a stored `javascript:` URL renders as struck-through text.

The browser may write only what the capability block above declares. **Approve**, **Reject**, **Re-approve** and **Withdraw approval** are here, in the approval packet itself; every command it may send is written out below. There are **no outward controls** — no Create draft and no Reconcile — because those are the actions that actually reach a mailbox, and authorizing one is not the same as performing it. Approvals is where the decision is taken: it shows the rows whose queue makes approval state relevant, with the bound packet and the approved-versus-now digests beside the controls that act on them.

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

Drafting and reconciling. `claim()`, `finish()`, `refuse()`, `reject()` and `OutwardActions` are all unreachable from this package, and a test fails the build if that changes. `decide()` and `BindingConflict` are reachable as of this release, and they are the only additions: the guard names what may be reached, so the next capability has to be argued for rather than arrive.

The active evaluation profile. The session panel reports only facts that are authoritative today, and which skills, locations and threshold a future intake would score against is launch configuration the engine does not store. Showing a label for it would mean inventing one. It arrives with the persisted-profile decision.

`.eml` ingestion and the Gmail label read, which are write affordances and belong with intake.

Outward authority. Creating a draft and reconciling one remain command-line actions, and they are the ones that actually reach a mailbox. Approving now happens here, bound to the packet it was read from; what an approval does is authorize, and authorizing is not the same as acting. That separation is the point rather than a staging accident: the operator says yes in one place, and the thing that leaves the machine is still started deliberately somewhere else.

Reversing a decision to decline. A rejected review can be approved again from the command line; this surface reports the decision and offers no control for it. A first write surface should not also be the place that quietly re-opens something an operator deliberately closed.

A readable database path. `/session` reports the path in full and the panel prints it in full, so a long one is the widest thing on the screen and pushes the layout around. The fix is to shorten it **in the middle**, keeping the start and the filename, since those are the parts that identify which database is open — and to keep the whole value reachable, as a title attribute and as selectable text, because a path the operator cannot read back is a fact the panel only appears to report. The truncation is presentation alone: the projection keeps serving the real value, and nothing downstream reads the shortened form.
