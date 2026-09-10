# CareerSignal

CareerSignal is a local recruiting application foundation: deduplicate opportunities, explain eligibility and fit, require explicit approval, and record controlled drafts and their audit trail in libSQL.

The application accepts structured opportunities, supplied text/HTML recruiting messages with explicit job fields, and messages read from an authorized Gmail mailbox. It creates Gmail drafts only from an explicitly approved review, under a separate compose credential, and does not send email, parse arbitrary alert layouts, or submit applications. See [message extraction](docs/message-extraction.md) for supported formats, local email-file ingestion, provenance, and limitations, and [Gmail intake](docs/gmail-intake.md) for the read-only mailbox adapter.

## Run locally

Use Python 3.11–3.14 (Windows: 3.11–3.13 with libSQL 0.1.11) and uv:

```sh
uv sync --locked
uv run careersignal init --db var/careersignal.db
uv run careersignal verify --db var/careersignal.db
uv run careersignal demo --db var/synthetic.db
```

Reading a mailbox is a separate, explicitly authorized command:

```sh
export CAREERSIGNAL_GMAIL_TOKEN=...
uv run careersignal gmail-ingest --mailbox operator@example.com --label Label_JobAlerts \
  --skill python --skill sql --db var/private.db
```

The Gmail *intake* adapter is read-only by construction: it defines no send, draft or modify operation, addresses one fixed host and two read URLs, refuses redirects, and takes its token from the environment rather than a flag. It creates no drafts and approves nothing, and it cannot address the drafts collection at all. Creating a draft is a separate adapter under a separate credential, described below. The token's scope is configured, not verified, and the tests drive recorded synthetic payloads rather than a live mailbox — see [Gmail intake](docs/gmail-intake.md) for exactly what is and is not established.

`demo` is an explicitly synthetic certification scenario, including a simulated operator approval. Use a separate demo database. Repeating it proves persisted receipt replay without creating another controlled draft. The provider is in-memory: a process restart loses its unrecorded drafts; unresolved attempts stay uncertain rather than being retried blindly.

Outside the demo, call `Workflow.intake`, inspect `Repository.review`, record a decision with `Repository.decide`, then call `Workflow.draft`. These are local trusted-caller APIs, not authenticated public endpoints. A changed review needs a new approval. A review recorded before stated skill coverage is not actionable after upgrading; re-ingest its opportunity under a new message ID to score it again. Incoming message IDs are immutable: reusing an ID with different content fails, while repeating the same message returns its original review references. Re-evaluation requires a new intake event.

Read what has accumulated with the operator views:

```sh
uv run careersignal opportunities --db var/private.db
uv run careersignal opportunities --db var/private.db --active --min-coverage 70
uv run careersignal opportunity <id> --db var/private.db
uv run careersignal opportunities --db var/private.db --json
```

These are read-only: they record no status, decide no review, create no draft and contact no mailbox. A table is printed by default and `--json` gives the same query result structured for other tools. The detail view also says whether an opportunity has been approved and whether a draft was refused, attempted, left uncertain or created, and shows the exact material an approval binds — review id, source message, recipient, subject and wording — so an outward draft is never approved unseen. See [operator views](docs/operator-views.md) and [the approval packet](docs/operator-interface.md#the-approval-packet).

Moving an opportunity through the search is one command, and it names the state it was decided against:

```sh
uv run careersignal opportunity <id> --db var/private.db            # read the status event
uv run careersignal status <id> --to applied --expect 12 \
  --actor operator --reason "Sent CV" --db var/private.db
```

If the opportunity moved since that event, nothing is written and the command says what it found. See [the operator interface](docs/operator-interface.md) for the whole command surface, and [status history](docs/status-history.md#compare-and-append) for the rule.

An approved review can become a real draft in an authorized mailbox. This is the only
capability that writes outside this machine, so it is never implicit:

```sh
uv run careersignal approve <review-id> --actor operator --db var/private.db
uv run careersignal draft <review-id> --db var/private.db          # in-memory provider

export CAREERSIGNAL_GMAIL_COMPOSE_TOKEN=...
uv run careersignal draft <review-id> --provider gmail \
  --mailbox operator@example.com --db var/private.db               # a real Gmail draft
```

The compose credential is separate from the read credential in every respect: its own
environment variable, its own scope, its own adapter and its own path allowlist. The
adapter defines no send operation and refuses the Gmail send endpoint by path. An approval
binds to the review, its exact wording and the status it was read in, and all three are
rechecked before the write; a recruiter-supplied address carrying a control character is
refused rather than repaired. See [approved drafts](docs/approved-drafts.md).

An opportunity also carries an operator-controlled status: `Repository.record_status`, `Repository.status` and `Repository.status_history`. The history is append-only and the current status is derived from it, never stored. An operator-facing write is a compare-and-append: it names the event it was decided against and refuses if the opportunity has moved since. See [status history](docs/status-history.md) for the vocabulary, what a status does not authorize, and the upgrade backfill.

The commands an operator reads print text by default, take `--json` for anything else, and report an outcome: `ACCEPTED` (exit 0), `REFUSED` (exit 1, nothing left this machine, correct it and ask again) or `UNCERTAIN` (exit 3, a provider was contacted and only reconciliation can say). A command written wrongly stays exit 2 on stderr, because that is a different thing from the system refusing.

Relative database paths resolve from the current working directory. Set `CAREERSIGNAL_DB_PATH` to override the default `var/careersignal.db`; explicit `--db` takes precedence. Keep personal runtime data outside the source tree or under ignored `var`.

## Verify

```sh
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run python ops/tools/verify_tree.py
uv run python ops/tools/verify_secrets.py
uv run python ops/tools/verify_wheel.py
```

The wheel verifier builds an artifact, installs it with dependencies into an empty temporary environment, checks imports and migration resources, and runs the synthetic workflow outside the checkout. See [verification evidence](docs/verification.md) for the tested scope and limitations.

Read [the build contract](docs/build-contract.md) for folder ownership, dependency rules, privacy requirements, and the gates before the first public commit. Coding agents must also follow [AGENTS.md](AGENTS.md).

The historical private repository is a reference for proven behavior. Its repository structure, runtime artifacts, and Git history do not belong in this new project.
