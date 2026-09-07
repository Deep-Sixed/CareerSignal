# Deterministic inbound extraction

PR #1 adds supplied-message ingestion. It does not connect to Gmail or introduce draft writes. The original structured JSON workflow and human approval boundary remain available.

## Supported contract

Plain-text recruiter messages and multi-job alerts use explicit fields, with one `Title:`, `Role:`, or `Job title:` starting each job:

```text
Hello candidate,
Role: Application Engineer
Company: Example Company
Location: remote
Skills: Python, SQL
URL: https://jobs.example.com/roles/1

Role: Support Analyst
Company: Example Company
Location: remote
Skills: SQL
Apply: https://jobs.example.com/roles/2
```

`Employer` aliases `Company`; `Apply` and `Job URL` alias `URL`. Skills are comma/semicolon-separated explicit values. Title, company and a valid HTTPS URL are required to create an opportunity. Missing location and skills remain unknown; existing eligibility/scoring rules decide whether the review advances. No skill or company is inferred from a title or prose.

HTML preserves visible labels and anchor targets, decodes entities, and turns h2–h4 headings into titles. Script/style/head/template contents and explicitly hidden elements are ignored. Labeled Apply/URL anchors use their href. No HTML is executed and no URLs, tracking redirects, external stylesheets or attachments are fetched. This is a limited structural extractor, not a browser renderer or a universal job-board parser. Unlabeled prose, unfamiliar layouts, and conflicting fields produce reviewable diagnostics rather than guessed opportunities. Headers/navigation may produce additional rejected items; provider-specific templates need independent fixtures before support is claimed.

Each job block is independent. A malformed block does not discard valid siblings. Conflicting versions of the same canonical URL in one message are rejected together; identical duplicates share the existing opportunity and retain separate extraction-item evidence.

## Identity and MIME

`Message` requires a caller-selected namespace identifying the source mailbox. Identity uses that namespace plus a supplied provider ID, otherwise RFC Message-ID, otherwise a normalized content fingerprint. Identical messages without either external ID intentionally deduplicate by content. The content digest includes normalized sender, subject, plain text and HTML. Reusing the same external identity with changed content raises the existing immutable-message error; it never silently overwrites evidence.

For supplied RFC email bytes, plain text is preferred over HTML; alternatives are not combined. Attachments and forwarded attached messages are ignored. Multiple inline bodies of one type, malformed MIME/encoding, unknown charsets, empty bodies and messages over 2 MB fail explicitly before storage. A failed MIME normalization does not produce a partial database intake. Message formats that cannot safely be interpreted need a later caller-side quarantine policy.

## Provenance and replay

Migration 0002 adds `message_sources` and `extraction_items` without changing the baseline schema or its checksum. Message metadata, parser version, each normalized source excerpt, rejection reason, and exact opportunity/review references are stored in the same transaction as intake. Raw MIME and attachments are not stored. Excerpts and sender/subject can contain personal information at runtime: keep the database private; public tests use only synthetic messages.

Replay returns persisted review references and preserves the original evidence. A parser/profile change does not reprocess an already-recorded message implicitly; use an explicit new event when re-evaluation is intended. Intake does not approve a review, create a draft intent, or call the provider.

## Use

```sh
uv run careersignal ingest --message /path/to/private-message.eml --namespace local-mailbox --skill python --skill sql --db /path/to/private.db
```

The CLI reads only the specified local file and reports review IDs plus per-item diagnostics. It has no Gmail credentials, network calls, draft action or send action. The API equivalent is `Workflow.intake_message(Message(...))`; use `Message.from_bytes` for MIME and `Repository.extraction_evidence` for persisted item evidence.

Future product sequence: read-only Gmail adapter; minimal CRM/status history; provider-backed approved drafts with reconciliation; then a small operator interface. Preserve Python >=3.11,<3.15, libSQL 0.1.11, and Ruff py311. Keep the historical repository reference-only. Develop through PR, independent approval and all 11 CI jobs before merge, then let actual recruiting usage determine further work.
