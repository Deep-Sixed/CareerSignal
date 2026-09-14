# Operator views

CareerSignal accumulates opportunities, evidence, reviews and status history. These commands make that accumulation readable. They are **read-only**: nothing here records a status, decides a review, creates a draft, contacts a mailbox, or alters evidence or history.

## The list

```sh
uv run careersignal opportunities --db /path/to/private.db
```

```
STATUS      COMPANY       TITLE               COVERAGE    ELIGIBLE
new         Northwind     Generalist          not scored  yes
reviewing   Example Corp  IAM Architect       100%        yes
interested  Contoso       SailPoint Engineer  75%         yes
rejected    Fabrikam      Desktop Support     33%         no
```

One row per **opportunity**, which is the durable business object. A review is evidence about an opportunity, so it appears as columns rather than as the subject of the list.

Rows are ordered by pipeline stage first — the status vocabulary's own order, `new` through `closed` — then by coverage descending, then by company and title. Ordering happens in the application rather than in SQL because the pipeline order is the domain's vocabulary, which the database does not know.

The table is deliberately five columns. Everything else a row carries is in the JSON form and in the detail view.

### Filters

```sh
careersignal opportunities --status reviewing
careersignal opportunities --active                       # searches still in progress
careersignal opportunities --eligible                     # location eligible
careersignal opportunities --ineligible
careersignal opportunities --min-coverage 70
careersignal opportunities --max-coverage 40
```

Filters compose. A status outside the vocabulary, a coverage bound outside 0–100, or `--eligible` together with `--ineligible` stops the command rather than quietly returning something else.

## One opportunity

```sh
careersignal opportunity <id>
```

Shows what it is, what the current review concluded — coverage, eligibility, whether it advances, and the full reasons — the draft wording an approval would bind to, and the whole status history with event id, actor, reason and timestamp.

It also reports where the opportunity stands as an action: whether it has been approved and who by, and whether a draft was refused, attempted, left uncertain or created. Alongside those it shows the exact material an approval binds — review id, source message, recipient, subject and wording — read from the same statement the authorization binds and re-verifies from. See [the approval packet](operator-interface.md#the-approval-packet).

## Structured output

```sh
careersignal opportunities --json
careersignal opportunity <id> --json
```

The table is what an operator reads; the JSON is what anything else consumes. A later web interface, another tool, or a script can use the same query layer without scraping terminal output. The JSON carries fields the table omits: `id`, `url`, `location`, `status_changed_at`, `matched_skills`, `stated_skills`, `advances`, and the review identifier.

## Reading the coverage column

| shown | means |
|---|---|
| `86%` | stated skill coverage for the current review |
| `not scored` | the job stated no skills, so there was nothing to cover |
| `pre-coverage` | the review predates stated skill coverage and cannot be approved or drafted |
| `-` | the opportunity has no current review |

The last two are distinct on purpose. A `pre-coverage` review is the upgrade case from migration 0003: it is not actionable and the operator needs to see that rather than a blank cell to interpret.

## Terminal safety

A job title comes from a recruiter's message. `recruiting.models.clean` collapses whitespace but leaves control characters intact, so a title such as `Engineer\x1b]0;spoofed\x07` reaches storage exactly as sent — and, printed raw, would retitle the terminal window. `\x1b[2J` would clear the screen.

Terminal-bound text is therefore escaped at the presentation boundary, in `system/views.py`, and nowhere else. C0, DEL and C1 control characters are rendered as `\x1b`-style text: visible to the operator rather than silently dropped, so they can see that a message contained one.

Two things this deliberately does **not** do:

- **It does not sanitize stored evidence.** The database keeps exactly what arrived, so the record of what a recruiter actually sent is never quietly rewritten. Only the printing is made safe.
- **It does not flatten text to ASCII.** `Zürich Söhne`, `東京` and `İstanbul` print unchanged. Safety comes from escaping control characters, not from restricting the alphabet.

A newline inside an operator's reason stays structural: the detail view prints each line on its own row, so a multi-line note remains readable while no single row can carry anything executable.

Only a line feed is structural, with `CRLF` folded into it. Python's `str.splitlines()` also breaks on bare `CR`, `VT`, `FF`, `FS`, `GS`, `RS`, `NEL` and the Unicode line separators, which would let a hostile control character disappear into structure rather than be escaped — the opposite of what this boundary promises. `U+2028` and `U+2029` stay in the line but are not escaped, because they are not terminal control sequences; they are outside the C0/C1 range this guards.

The `--json` form needs none of this — `json.dumps` escapes control characters already — and it retains the underlying value, so automation still sees exactly what was stored.

## Read projections

Five reads exist for a graphical surface that does not exist yet. They have **no command**:
nothing below is reachable from the CLI, and adding one is a separate change.

| | |
|---|---|
| `views.queue(summary, action)` | Which queue an opportunity is in: `reconcile`, `created`, `stale`, `draft`, `decide`, `rejected`, `none` |
| `Repository.communications()` | Every ingested message, in arrival order, with its extracted and unextracted counts |
| `Repository.communication(message_id)` | One message, its per-item evidence, and the opportunities it currently addresses |
| `Repository.sources(opportunity_id)` | Every message an opportunity arrived in, oldest first |
| `Repository.timeline(limit=200, since=None)` | Status events and audit events as one stream, newest first |

They derive nothing new. Each one reads what a write path already decided, so a list and a
detail pane cannot disagree: `queue()` is a pure function over the two dictionaries the
repository already returns, the message counts are the same evidence rows
`extraction_evidence()` returns, and `sources()` ends at the message `_addressing()` binds
because it uses that join and that ordering rather than a second opinion.

Three orderings are load-bearing:

- **Arrival is a row number, not a clock.** `communications()` and `sources()` order by the
  row each insert assigned, the same sequence that decides which message addresses a draft.
  Two messages can share a second; they cannot share a row.
- **`queue()`'s test order is the rule.** A durable intent wins absolutely, so a
  `draft_refused` in the audit trail beside an open intent describes a draft that is not
  currently proposed rather than the current state.
- **`timeline()`'s `since` is an inclusive lower bound, not a cursor.** `created_at` has
  second resolution, so an exclusive bound would silently drop an event sharing its second
  with the last one a caller saw; an inclusive one can repeat that event instead, and a
  repeat is recoverable where an omission is not. Paging backwards needs a composite
  `(created_at, id)` cursor or its own parameter — not a narrowing of this one.

`timeline()` unions two append-only ledgers with independent id sequences, so each row says
which ledger it came from: `kind` with `event_id` is the identity, and `event_id` alone is
not unique across the stream.

## What these commands are not

- They do not change status. That is the `status` command, which is a [compare-and-append](status-history.md#compare-and-append) write and a different command on purpose.
- They do not approve, reject or draft anything. Drafting still requires an explicit decision against a specific review version.
- They do not read Gmail, and they build no provider at all. A test refuses to let any reporting path construct a Gmail reader, a Gmail draft writer or the controlled provider.

## Limitations

- **No paging, colour, or interactive display.** Long titles are never truncated, so a wide row wraps in a narrow terminal. Presentation beyond this belongs to the operator interface, not here.
- **Ordering and filtering are not indexed for scale.** Rows are sorted in the application after the query returns. That is honest for a personal search and would need revisiting for a database orders of magnitude larger.
- **One status filter at a time.** `--status` takes a single value; `--active` covers the common "still in progress" case.
- **Runtime data is personal.** Company, title, URL and status reasons come from a real mailbox at runtime. Keep the database private; public fixtures are synthetic.
