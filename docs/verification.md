# Baseline verification

Local environment: Windows, CPython 3.13.13. Status before initial publication: local checks passed; hosted CI pending.

| Gate | Evidence |
|---|---|
| Fresh workspace | New directory, no historical Git metadata; initialize Git only after local gates pass. Destination repository ID must be 1360005791. |
| Scope/privacy | Source written locally; reuse ledger records the single inspected storage reference. Tree scanner checks ownership, imports, case collisions, text artifacts and email domains. Targeted private-identity and machine-path patterns supplied only via process environment, not published. No resume documents, personal messages, credential files, or binary archives in the publication inventory. |
| Secret detection | detect-secrets 1.5.0 scans every publishable file with network verification disabled. One exact synthetic credential URL in an unsafe-URL rejection test is explicitly allowlisted; it is not a real account. No broad exclusions for source files. |
| Tests | 20 tests: normalization, distinct jobs within one message, message/opportunity dedupe, strict score threshold, eligibility, stale approvals, transaction rollback/commit, foreign keys, migration repeat/failure/upgrade/checksum, two-connection retry/exhaustion, concurrent migrations, immutable audit, concurrent draft claims, uncertain external results and receipt reconciliation. |
| Static quality | Ruff check and format check. Automated source import-direction checks. |
| Installed artifact | Wheel installed with dependencies into an empty temporary venv. pip check; all four module origins resolve under site-packages. Migration included, tests/fixtures excluded. Blank initialization, database integrity/foreign-key/migration checks, golden workflow and replay execute with Python isolated mode outside the checkout. |

Reproduce checks using README commands. Hosted CI repeats the gates across 11 OS/Python combinations: Linux/macOS 3.11–3.14 and Windows 3.11–3.13. Windows/Python 3.14 remains the explicit libSQL exception. Local success does not establish results on other operating systems; consult the actual GitHub run after publication.

## Limits

This is a controlled application foundation, not completed live Gmail functionality. Inputs are structured synthetic jobs; scoring is a transparent configured skill ratio with a location eligibility gate. The caller records an explicit decision against an immutable review version. There is no web authentication layer or sending operation. The demo simulates the human decision step and labels its output accordingly.

The in-memory draft provider cannot recover its own state after process exit. A durable confirmed receipt replays safely; an attempt without a durable receipt remains uncertain and cannot be automatically retried. A future Gmail adapter must implement provider-specific reconciliation before claiming equivalent recovery. No personal data transfer or production mailbox validation was performed.
