# CareerSignal build contract

Status: adopted build boundary. Implementation and gate evidence are tracked in verification.md.

This contract supersedes the old in-place Nexus migration, the PR #11–#20 folder-cleanup program, and proposals to copy the historical repository wholesale. Build a small new application and selectively reuse demonstrated behavior.

## 1. Product scope

CareerSignal processes subscribed Gmail job alerts and recruiter messages. An alert can contain multiple jobs; each opportunity must be extracted and deduplicated independently. Preserve source provenance so the operator can inspect why a recommendation was made.

The baseline workflow is:

```text
synthetic email / authorized Gmail intake
  → extract and normalize opportunities
  → deduplicate
  → eligibility checks
  → explainable fit score
  → review packet
  → explicit human approval
  → controlled draft creation
  → evidence, audit, receipt
  → safe replay
```

Default processing threshold is strictly above 70 on a 0–100 fit scale. Make it configurable. Hard eligibility failures override the score. Missing information must remain visible rather than being invented. A fit score is not a probability of getting hired. Below-threshold items need a disposition and explanation but do not advance to draft preparation automatically.

In scope: opportunity intake, recruiting decisions, communications, minimal CRM/history, evidence, approvals, drafts, and the operator tools required for that workflow.

Out of scope for the baseline: job-board scraping, automatic sending, automatic application submission, generic multi-agent platforms, unrelated observability fleets, model-hosting systems, infrastructure management, and speculative plugin ecosystems. Live Gmail reads and draft writes require explicitly configured authorization. Public CI uses synthetic inputs and controlled adapters, never a personal mailbox.

## 2. One folder scheme

The following is the permitted target, not an instruction to create empty directories:

```text
CareerSignal/
├── src/
│   ├── recruiting/
│   │   └── tests/              # synthetic domain tests/fixtures
│   ├── communications/
│   │   └── tests/              # adapter contracts and synthetic email
│   ├── data/
│   │   ├── migrations/        # canonical packaged SQL migrations
│   │   └── tests/              # real SQLite and concurrency tests
│   └── system/
│       └── tests/              # API, integration, golden workflow, wheel checks
├── ops/                       # only necessary operational assets
│   ├── config/                # safe example configuration
│   ├── tools/                 # maintenance/build/verification entry points
│   ├── docker/                # only if a real container use case exists
│   └── services/              # only if service definitions are required
├── docs/                      # concise maintained product/build documentation
├── .github/
│   └── workflows/
├── var/                       # ignored local runtime data, never packaged
├── .gitignore
├── AGENTS.md
├── README.md
├── pyproject.toml
└── uv.lock
```

Use `src`, not `scr`. No `src/nexus`, `src/careersignal`, or repeated project-name layers. Root files are limited to project metadata, policy, and tool configuration. No duplicate root migrations, tests, scripts, or alternative source trees. `docs` remains flat until an actual collection needs grouping; do not create four documentation hierarchies in anticipation of future work.

| Owner | Belongs here | Does not belong here |
|---|---|---|
| `recruiting` | Opportunity types, normalization, dedupe rules, eligibility, scoring, review/approval rules, repository interfaces | Gmail clients, SQL statements, API routes |
| `communications` | Gmail adapter, message decoding, draft rendering/delivery adapter, provider receipt translation | Fit scoring, approval decisions, direct business SQL |
| `data` | SQLite connection/transactions, repositories, relational schemas, migrations, durable audit/idempotency state | Mailbox calls, HTTP routes, UI logic |
| `system` | Composition, configuration, use-case orchestration, CLI/API, minimal operator UI, end-to-end checks | Duplicate domain rules or generic platform infrastructure |
| `ops` | Tools and operational examples needed to build/run/verify CareerSignal | Importable application logic, private machine configuration |
| `docs` | How the product works, decisions, setup and operations | Conversation dumps, personal application packets, machine biographies, backup trees |

Start with files inside these owners. Create deeper capability packages only when their files form a coherent unit. Keep any initial operator UI under `system`; a future separate frontend requires a documented ownership/build decision, not an automatic new `apps` hierarchy.

Names must work on case-insensitive filesystems. No files distinguished only by capitalization, compatibility symlinks, or `sys.path`/`PYTHONPATH` workarounds. `data` and `system` are deliberately short import names; check clean-install imports resolve to this wheel rather than an unrelated dependency.

## 3. Dependency direction

`recruiting` is the domain core and owns the interfaces it needs. Keep it independent of Gmail, the concrete database engine, and the application shell.

```text
system → recruiting, communications, data
communications → recruiting interfaces/types
data → recruiting interfaces/types
recruiting → standard library and deliberately selected domain dependencies
```

`recruiting` must not import `system`, `data`, or `communications`. Communications and data do not import one another. `system` wires adapters to domain interfaces and coordinates persistence and external actions. Do not hold database write transactions open across Gmail/network calls.

Tests live with their owning package; cross-component tests live under `system/tests`. Reusable fixture helpers go in the narrowest owning test package. Keep test-only imports out of runtime modules. Establish an automated import-boundary check before the first public baseline.

## 4. Persistence and packaging

- Python `>=3.11,<3.15`; the runtime database engine is Python's standard-library `sqlite3`, with no third-party runtime database dependency.
- SQLite `>=3.38.0` is required because the canonical migration history uses `unixepoch()`; refuse before filesystem mutation when the interpreter carries an older runtime.
- Local relational database; preserve meaningful foreign keys, unique keys, checks, and transaction semantics. JSON is for non-critical metadata, not a replacement for relations.
- Foreign-key enforcement on every connection and verified WAL behavior for file-backed databases.
- One transaction boundary with bounded `BEGIN IMMEDIATE` retry. Test commit, rollback, real two-connection contention, and retry exhaustion.
- SQL migrations live only in `src/data/migrations`, are packaged resources, and are discovered with `importlib.resources`. Maintain a migration ledger; test blank initialization, repeat invocation, rollback on failure, and upgrades as versions are introduced.
- Default runtime path may be `var/careersignal.db`, with a configurable `CAREERSIGNAL_DB_PATH`. Resolve/document its base explicitly; never write into installed package directories. Tests always use temporary paths.
- No PostgreSQL or PostgreSQL drivers/provisioning in this new runtime. Any eventual legacy-data transfer is a separate private, reconciled operation, not a dependency of clean startup.
- Configure wheel contents explicitly: the four source packages and required runtime resources. Exclude tests, fixtures, ops, runtime data, archives, and private files. Do not accidentally exclude SQL migrations.
- The built wheel must remain platform-neutral (`py3-none-any`) and declare no runtime dependencies while the application uses only standard-library runtime facilities.
- Install a built wheel into an empty environment; run outside the repository with no source-path assistance. Verify module origins, packaged migrations, blank database setup, database contract, and the synthetic workflow.

**Storage decision — 2026-09-13.** CareerSignal uses Python's standard-library `sqlite3` as its production storage engine. The previous exact `libsql==0.1.11` dependency was removed after the complete application and storage suite ran successfully against stdlib SQLite, while libSQL's Python binding imposed a native-wheel/platform dependency without a CareerSignal feature using its extensions. The standard-library choice restores the full Linux/macOS/Windows Python 3.11–3.14 matrix, keeps mature SQLite WAL and multi-process semantics, and makes the wheel independent of a database extension package. `src/data/store.py` remains the only runtime module allowed to import a database engine. Revisit this decision only when CareerSignal has a concrete requirement for a capability such as managed synchronization or embedded replicas, and only after the candidate engine independently passes the complete storage, concurrency, migration, installed-wheel and supported-platform gates. Runtime SQLite version is reported explicitly; the WAL-reset fix status is diagnostic rather than a higher installation floor because patched versions are not yet uniformly bundled by supported Python distributions.

## 5. Approval, idempotency, and recovery

Deduplication of an incoming message, deduplication of an opportunity, and idempotency of draft creation are separate contracts. Specify keys and replay behavior for each. Approval must identify the exact review/draft version it authorizes; changed content invalidates prior approval.

Record intent before an external draft operation and retain provider receipts for reconciliation. An uncertain result must enter a recoverable state; do not blindly retry and create duplicate drafts. `BEGIN IMMEDIATE` does not make a remote Gmail operation exactly-once. Test failure before the call, after remote success but before local receipt persistence, and repeated delivery of the same input.

The local baseline can use deterministic scoring and a controlled in-memory communications adapter. It must still exercise real SQLite persistence, explicit approval, audit records, and replay behavior end to end. Do not use a mock to claim live Gmail integration is verified.

## 6. Selective reuse and privacy

Use the private historical repository as reference only. For every selected component record its product requirement, source revision/path, destination owner, dependency changes, privacy inspection, and behavior checks in a compact reuse ledger under docs. Preserve applicable licenses and attribution without exposing personal data. Reuse is justified by useful behavior, never age or prior architectural prominence.

Do not import old `.git` directories, commits, tags, branches, Git bundles, databases, resumes, personal messages, attachments, real Gmail identifiers, personal email defaults, home/LAN paths, credential stores, or infrastructure backups. No copied conversation logs or old agent instruction files. Public examples use reserved example domains and synthetic identities. Credentials belong in environment variables or an appropriate private credential store, not committed configuration.

Inspect text, filenames, binary files, and archive contents. Run secret detection as well as targeted identity checks; review findings rather than treating every mention of `token` or `password` as a secret. `.gitignore` is defense in depth, not proof of privacy. Scan the exact staged/publishable tree and built artifacts.

## 7. Gates before the first public commit

Record commands, artifact/source identifiers, results, and unresolved limitations in verification.md. No gate passes merely because its script exists.

1. **Fresh ancestry:** new directory and newly initialized Git repository; no inherited commits. Confirm intended public destination by repository ID `1360005791` as well as name. Historical repository ID `1233533500` is reference only. Never push from an old clone.
2. **Scope/tree:** every source file has an owner; dependency direction is checked; no legacy infrastructure, duplicate implementations, or case collisions. The database-engine import is confined mechanically to `src/data/store.py`.
3. **Privacy:** reviewed identity/secret scans, binary/archive inventory, and synthetic fixtures; inspect the exact publication tree and wheel contents.
4. **Storage:** real SQLite contract, migrations, constraints, transaction/concurrency and retry tests pass.
5. **Installed artifact:** wheel builds as `py3-none-any`, declares no runtime dependencies, installs into an empty environment, passes dependency consistency, imports from that installation, includes migrations, excludes private/test/runtime assets, and runs outside the checkout.
6. **Golden workflow:** synthetic intake through explicit approval, controlled draft, audit/receipt, and replay passes using real local storage. Rejected/ineligible and unapproved inputs cannot create drafts.
7. **Quality/CI readiness:** relevant tests and Ruff pass locally; workflow definitions cover all twelve Linux/macOS/Windows and Python 3.11–3.14 combinations. Each hosted leg reports the Python and SQLite runtime it actually carries. Clearly distinguish locally tested environments from pending hosted CI.
8. **Publication:** only after gates 1–7 pass, create the clean first commit with Deep-Sixed and the GitHub noreply identity. Publish, then independently verify GitHub's tree, ancestry, visibility, and hosted CI. Do not claim hosted CI passed before it runs. Fix failures before feature PRs begin.

No request for another confirmation is implied by these gates when publication has already been authorized. Failed gates require fixes or an explicit user-approved change to the contract, not misleading claims of completion.

## 8. Delivery after the baseline

The first baseline must already contain the smallest useful synthetic golden workflow. This latest requirement supersedes an earlier proposal to postpone the whole workflow until a later PR.

After the baseline, use small PRs to expand recruiting coverage; complete CRM/evidence/audit behavior; add the real Gmail adapter and approved drafts; improve API/operator usability; and broaden portability/recovery certification. Numbering is a work plan, not a quota, and no PR numbers are reserved yet. Keep existing checks passing throughout.

Any temporary exception must name an owner, explain why it exists, state its containment and removal condition, and identify an expiration milestone or PR. No indefinite temporary paths. Separate mechanical porting from business-rule redesign.

Keep the old repository private and unarchived until the new baseline and hosted checks are verified. Archive later as a deliberate lifecycle action. Evaluate success by worthwhile opportunities surfaced, unsuitable jobs rejected, reduced email workload, draft editing effort, understandable decisions, and maintenance cost.
