# Read-only Gmail intake

CareerSignal can read an authorized Gmail mailbox and turn its messages into review packets. It cannot send, reply, draft, label, delete, or modify anything. That is a property of the code, not a policy: `communications/gmail.py` defines no write operation and the only network helper fixes its method to `GET`.

## What the adapter may address

Two URLs exist for this adapter, both on one fixed host:

```text
https://gmail.googleapis.com/gmail/v1/users/me/messages
https://gmail.googleapis.com/gmail/v1/users/me/messages/{id}?format=raw
```

`readable_url` refuses everything else before a bearer token is attached: a different host, a look-alike host, plain HTTP, a non-default port, userinfo in the URL, any path outside the allowlist, and any path whose final segment names a Gmail operation or collection rather than a message (`send`, `drafts`, `modify`, `trash`, `batchDelete`, `settings`, and the rest). Message identifiers are separately validated against `[0-9A-Za-z_-]{1,128}` and the same reserved list, so neither a malformed identifier nor one Gmail itself returned can be spelled into a mutation URL.

Redirects are refused rather than followed. Following one would re-send the bearer token to whatever host the response named.

## Authorization

The access token is read from the `CAREERSIGNAL_GMAIL_TOKEN` environment variable and never from a command-line flag, because a command line is visible in shell history and to other users of the machine. CareerSignal does not perform the OAuth flow, does not store the token, and does not refresh it; obtaining and rotating it is the operator's responsibility.

`GmailCredentials` accepts exactly one scope, `https://www.googleapis.com/auth/gmail.readonly`, and refuses a broader or additional grant instead of quietly narrowing it. Be clear about what this does: it constrains what CareerSignal is configured to ask for. It does not inspect or constrain the token it is handed. A token that carries write scopes is still a write-capable token; the guarantee that nothing is written comes from the absent write path, not from this check.

The token appears only in an `Authorization` header. It is never placed in a URL or query string, never included in an error message, and `GmailCredentials` redacts it from its own representation so it cannot reach a log line, a traceback frame dump, or a captured test report.

## Provenance

The Gmail message identifier is the provenance. It is immutable, unique within a mailbox, and recorded as the `external_id` in `message_sources`.

The RFC822 `Message-ID:` header is deliberately not used for identity. That header is written by whoever sent the mail, and two different messages can carry the same one; trusting it would let a sender merge two distinct messages or make one impersonate another.

The namespace is `gmail:<mailbox>`, with the mailbox case-folded, because Gmail addresses are not case sensitive and two spellings of one mailbox must not split the same message into two identities. Changing the mailbox string changes every message key derived from it.

## Replay

Reading the same mailbox twice produces the same message keys, so intake returns the existing review references and writes no new message, opportunity, review, or draft intent. A provider identifier that reappears carrying different content is refused with the existing immutable-message error rather than overwriting evidence.

Reading a mailbox never creates a draft intent, never calls the draft provider, and never approves anything. The human approval boundary is unchanged: a review still requires an explicit `Repository.decide` before `Workflow.draft` will do anything.

No write transaction is held open across a mailbox read. The whole batch is read first, then ingested.

## Use

```sh
export CAREERSIGNAL_GMAIL_TOKEN=...        # authorized read token, not stored by CareerSignal
uv run careersignal gmail-ingest \
  --mailbox operator@example.com \
  --label Label_JobAlerts \
  --query "newer_than:7d" \
  --limit 25 \
  --skill python --skill sql \
  --db /path/to/private.db
```

The command prints the mailbox namespace, how many messages were read, and each message's provider identifier, message key and review IDs. It does not print message content. The API equivalent is `GmailReader(GmailCredentials(token, mailbox)).messages(...)` followed by `Workflow.intake_message` per message.

## Limitations

These are real and worth knowing before this path is trusted with an uncontrolled mailbox.

- **The tests do not prove live Gmail works.** Every test drives the adapter through recorded synthetic payloads over an injected transport. They establish the adapter's contract, its refusals, and its replay behaviour. They do not establish that Google's live API behaves as recorded, and no personal mailbox has been read. Public CI never contacts a mailbox.
- **One unreadable message stops the batch.** Message-level failures are all-or-nothing, matching the existing envelope rule: a body that cannot be parsed is not a message. Per-item independence applies to jobs inside a message, not to messages inside a mailbox. Narrow the query to skip a message that cannot be read.
- **No incremental sync.** Each run lists messages afresh; `historyId` is not tracked. Bound the work with `--query` and `--limit`.
- **No retry or backoff.** A 429 or 5xx is reported, not retried. Rate-limit handling is left to the operator so that no read is silently repeated.
- **Extraction quality is unchanged.** Real alerts use provider-specific layouts. The extractor is the same limited structural one documented in [message extraction](message-extraction.md); unfamiliar layouts produce reviewable diagnostics rather than guessed opportunities.
- **Runtime data is personal.** Sender, subject and extraction excerpts from a real mailbox are stored in the local database. Keep it private; public fixtures are synthetic.
