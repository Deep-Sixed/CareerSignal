# Product screenshots

One file lives here: `dashboard.png`, the image the repository's front page shows.

**It is a capture of the running application**, taken from a real `careersignal serve`
against a real SQLite database, at 1280×840. The pipeline counts, the work queues, the
activity entries and the session panel are what that build actually rendered — not a design
comp, not an edited image, and not a promise about a version that does not exist yet.

**Every value in it is synthetic.** The database was built by running the shipped commands —
`ingest`, `status`, `approve`, `reject`, `draft` — over invented recruiting alerts. The
companies are fictional, every address and URL uses an RFC 2606 reserved domain, the
provider is the local controlled one, and both Gmail credentials read `absent` because
neither was configured. Nothing here came from anybody's mailbox.

## This is not the design baseline

`docs/ui-design/` holds something different and must not be confused with it: the **frozen
design artifacts**, accepted 2026-09-13, plus `renders/*.png` exported from Claude Design so
that design can be viewed from a clone. Those renders are the accepted *design*. Their
wordmark reads `MOCK`; this screenshot's reads `LOCAL`, which is the difference.

The distinction is not pedantry. PR #37 originally put `docs/ui-design/renders/dashboard.png`
on the front page. Beside its fabricated data, that render states:

> reconciliation reads the drafts collection for this review's intent key and *writes
> nothing new*

`"writes nothing new"` is in `FORBIDDEN_CLAIMS` in `src/system/tests/test_web_frontend.py`,
because it is false: `reconcile()` calls `finish()`, which updates the intent row and appends
an audit event. It creates no second draft; it does not write nothing. The shipped browser is
mechanically forbidden from saying it. Publishing the render would have made that the first
sentence a visitor read.

## Adding another one

`ops/tools/verify_tree.py` admits this file **by exact path**, not this directory. A second
screenshot stops the gate until somebody widens the allowance deliberately — which is the
intent. A picture on the front page is a claim about what CareerSignal does, and the claim
gets reviewed.
