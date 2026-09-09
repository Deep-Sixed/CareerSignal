# CareerSignal

CareerSignal is a local recruiting application foundation: deduplicate opportunities, explain eligibility and fit, require explicit approval, and record controlled drafts and their audit trail in libSQL.

The application accepts structured opportunities, supplied text/HTML recruiting messages with explicit job fields, and messages read from an authorized Gmail mailbox. It uses an in-memory draft provider and does not send email, create Gmail drafts, parse arbitrary alert layouts, or submit applications. See [message extraction](docs/message-extraction.md) for supported formats, local email-file ingestion, provenance, and limitations, and [Gmail intake](docs/gmail-intake.md) for the read-only mailbox adapter.

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

The Gmail adapter is read-only by construction: it defines no send, draft or modify operation, addresses one fixed host and two read URLs, refuses redirects, and takes its token from the environment rather than a flag. It creates no drafts and approves nothing. The token's scope is configured, not verified, and the tests drive recorded synthetic payloads rather than a live mailbox — see [Gmail intake](docs/gmail-intake.md) for exactly what is and is not established.

`demo` is an explicitly synthetic certification scenario, including a simulated operator approval. Use a separate demo database. Repeating it proves persisted receipt replay without creating another controlled draft. The provider is in-memory: a process restart loses its unrecorded drafts; unresolved attempts stay uncertain rather than being retried blindly.

Outside the demo, call `Workflow.intake`, inspect `Repository.review`, record a decision with `Repository.decide`, then call `Workflow.draft`. These are local trusted-caller APIs, not authenticated public endpoints. A changed review needs a new approval. A review recorded before stated skill coverage is not actionable after upgrading; re-ingest its opportunity under a new message ID to score it again. Incoming message IDs are immutable: reusing an ID with different content fails, while repeating the same message returns its original review references. Re-evaluation requires a new intake event.

Read what has accumulated with the operator views:

```sh
uv run careersignal opportunities --db var/private.db
uv run careersignal opportunities --db var/private.db --active --min-coverage 70
uv run careersignal opportunity <id> --db var/private.db
uv run careersignal opportunities --db var/private.db --json
```

These are read-only: they record no status, decide no review, create no draft and contact no mailbox. A table is printed by default and `--json` gives the same query result structured for other tools. See [operator views](docs/operator-views.md).

An opportunity also carries an operator-controlled status: `Repository.record_status`, `Repository.status` and `Repository.status_history`. The history is append-only and the current status is derived from it, never stored. See [status history](docs/status-history.md) for the vocabulary, what a status does not authorize, and the upgrade backfill.

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
