# CareerSignal UI design baseline

**Accepted / frozen: 2026-09-13.** The design phase is closed. These artifacts are the
implementation inputs for the graphical interface over the existing CareerSignal engine.

## What is here

| File | Standing |
|---|---|
| `CareerSignal-Mock.dc.html` | **Authoritative.** The frozen reference build: five navigation items (Dashboard, Inbox, Opportunities, Approvals, Activity), a three-pane shell, and one detail pane with two shapes (opportunity, communication). |
| `CareerSignal-Handoff.dc.html` | **Authoritative.** Design-to-implementation handoff: field-by-field backing for every displayed value, the minimal new read projections, the single write-side change, and the PR sequence. |
| `CareerSignal-UI-Review.dc.html` | Supporting rationale for the two above. It records why the interface is shaped this way; it does not decide anything they do not. |
| `renders/*.png` | Convenience snapshots, exported from Claude Design itself. Not a design source. |
| `SHA256SUMS` | Digests for the three frozen Design artifacts and five static renders, so a clone can prove it holds the accepted baseline. |

Changes to the three `.dc.html` files from here are bugs against the handoff, not design
iterations. They are frozen exports: they are committed byte for byte, and they are not
edited to satisfy tooling. When a publication gate and a frozen artifact disagreed, the gate
was corrected instead — see `ops/tools/verify_tree.py` and `ops/tools/verify_secrets.py`,
whose reviewed allowances name these files explicitly.

## Why the renders exist

The `.dc.html` exports are evidence and source, not standalone pages. Each one loads the
Claude Design canvas runtime (`support.js`) and an Industry design-system bundle
(`_ds/industry-*`), neither of which is redistributed in this repository. Opened from a
clone without them, an artifact renders as unbound template expressions rather than as the
design.

The PNGs under `renders/` exist only so the accepted design can be *viewed* from a clone.
They are exported from Claude Design — the same renderer in which the design was accepted —
so they show what was actually approved. A locally reconstructed renderer was used during
review to inspect the mock, and it was useful for that, but it diverged from the real
runtime in at least two places (a boolean attribute binding, and table row expansion), so
nothing it produced is committed here. Where a render and an artifact disagree, the artifact
is authoritative.

## Deliberately unresolved

Two decisions belong to their own contracts and are not settled by this baseline. Neither
blocks the first implementation PRs, and neither should be smuggled into a UI change:

- **Web packaging.** Stdlib-only, an optional `careersignal[web]` extra, or a formal
  exception to the zero-runtime-dependency rule in the build contract.
- **Inbox clock.** `received_at` (the mailbox's clock) versus `ingested_at` (this machine's).
  They disagree exactly when it matters, on a batch import. Until one is chosen the Inbox
  orders by arrival and says so.

## Implementation notes recorded against the baseline

Found while reviewing the frozen artifacts. They are recorded here rather than by editing
the artifacts, which are closed:

- **Inbox intake.** The mock has no ingest affordance anywhere — `ingest` appears only in
  explanatory copy — yet the release is defined by a job seeker ingesting a week of alerts.
  The Inbox must expose both `.eml` ingestion and the Gmail-label read action.
- **Session facts must name the active evaluation profile.** The session panel shows the
  database path, token presence and mailbox. Once the interface can ingest, which skills,
  locations and threshold a new review is scored against comes from launch arguments the
  operator cannot see. Stored `advances` on existing rows is unaffected; this bites only on
  intake, which is exactly what the intake work adds.
- **Inbox sender presentation.** At list width a sender truncates to `bob.source…` or
  `Jane Recrui…`, dropping the domain — the part that carries trust for an untrusted `From`,
  and the part that distinguishes two messages from the same display name. Preserve the
  address or truncate from the middle; do not truncate the domain away.

## What happens next

Implementation begins with the behaviour-neutral `Workflow` split: `Intake(repository,
profile)` and `OutwardActions(repository, provider)`, which deletes the
`Profile(("placeholder",))` the draft path carries today. The method bodies do not change.
The handoff's section 6 carries the full PR sequence.

The `queue()` precedence invariant carries forward unchanged: an intent wins absolutely over
audit-derived states, so Dashboard queue classification and the detail pane cannot disagree
about the same opportunity.
