# CareerSignal

CareerSignal is a local recruiting application foundation: deduplicate opportunities, explain eligibility and fit, require explicit approval, and record controlled drafts and their audit trail in libSQL.

The application accepts structured opportunities and supplied text/HTML recruiting messages with explicit job fields. It uses an in-memory draft provider and does not read Gmail, send email, parse arbitrary alert layouts, or submit applications. See [message extraction](docs/message-extraction.md) for supported formats, local email-file ingestion, provenance, and limitations.

## Run locally

Use Python 3.11–3.14 (Windows: 3.11–3.13 with libSQL 0.1.11) and uv:

```sh
uv sync --locked
uv run careersignal init --db var/careersignal.db
uv run careersignal verify --db var/careersignal.db
uv run careersignal demo --db var/synthetic.db
```

`demo` is an explicitly synthetic certification scenario, including a simulated operator approval. Use a separate demo database. Repeating it proves persisted receipt replay without creating another controlled draft. The provider is in-memory: a process restart loses its unrecorded drafts; unresolved attempts stay uncertain rather than being retried blindly.

Outside the demo, call `Workflow.intake`, inspect `Repository.review`, record a decision with `Repository.decide`, then call `Workflow.draft`. These are local trusted-caller APIs, not authenticated public endpoints. A changed review needs a new approval. Incoming message IDs are immutable: reusing an ID with different content fails, while repeating the same message returns its original review references. Re-evaluation requires a new intake event.

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
