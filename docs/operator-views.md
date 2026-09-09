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

Shows what it is, what the current review concluded — coverage, eligibility, whether it advances, and the full reasons — the proposed draft text, and the whole status history with actor, reason and timestamp.

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

The `--json` form needs none of this — `json.dumps` escapes control characters already — and it retains the underlying value, so automation still sees exactly what was stored.

## What these commands are not

- They do not change status. That is `Repository.record_status`, and a command for it is deliberately later work.
- They do not approve, reject or draft anything. Drafting still requires an explicit decision against a specific review version.
- They do not read Gmail. A test refuses to let any query path construct a Gmail reader.

## Limitations

- **No paging, colour, or interactive display.** Long titles are never truncated, so a wide row wraps in a narrow terminal. Presentation beyond this belongs to the operator interface, not here.
- **Ordering and filtering are not indexed for scale.** Rows are sorted in the application after the query returns. That is honest for a personal search and would need revisiting for a database orders of magnitude larger.
- **One status filter at a time.** `--status` takes a single value; `--active` covers the common "still in progress" case.
- **Runtime data is personal.** Company, title, URL and status reasons come from a real mailbox at runtime. Keep the database private; public fixtures are synthetic.
