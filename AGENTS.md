# CareerSignal build rules

The authoritative build contract is [docs/build-contract.md](docs/build-contract.md). Read it before implementation. Explicit user instructions take precedence; record accepted changes in the contract instead of creating competing guidance files.

## Required boundaries

- This is a fresh product workspace. Never import old Git history, clone metadata, tags, branches, or recovery bundles. Historical repositories are reference only.
- Keep the public repository empty until every first-publication gate in the contract passes locally. Consult docs/verification.md for evidence; do not infer a gate passed from the presence of implementation code.
- Source belongs under `src/recruiting`, `src/communications`, `src/data`, or `src/system`. Do not add a project-name wrapper under `src`.
- Each capability has one canonical implementation and owner. Do not create parallel `apps`, `services`, `profiles`, `datacore`, `nexus-agent`, root `scripts`, or root `tests` trees.
- Add folders when they contain required work. Do not build placeholder hierarchies, generic frameworks, compatibility aliases, or a catch-all `utils` package.
- Public fixtures must be synthetic. Personal resumes, messages, account identifiers, credentials, databases, and host configuration stay outside tracked source.
- Preserve human approval before draft creation. Sending email or submitting applications is outside the baseline scope.
- Use Python `>=3.11,<3.15` and `libsql==0.1.11`. No PostgreSQL, psycopg, pg0, pg_jsonschema, or external database server in the new runtime.
- Validate the installed wheel outside the checkout with dependencies and no source-path assistance. Include migrations; exclude tests and fixtures.
- Keep the synthetic golden workflow passing as functionality grows. Report exactly which gates passed and which remain incomplete.
- Reuse old code only after documenting the product requirement, new owner, dependencies, privacy review, and behavioral checks. No wholesale tree copy.
- Temporary exceptions require a named owner, reason, containment, removal condition, and expiration milestone/PR. An exception does not silently waive a first-publication gate.
- Before pushing, verify the destination is the NEW repository ID `1360005791`. Use `Deep-Sixed` and `281237415+Deep-Sixed@users.noreply.github.com` for attribution. Recheck GitHub after publication.

Do not modify unrelated repositories or historical clones as a side effect of building this workspace.
