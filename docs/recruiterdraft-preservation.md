# RecruiterDraft preservation and integration boundary

Source: Jarvis `/mnt/jarvis-data/projects/labs/RecruiterDraft/backend/recruiterdraft/`
(original Stage 1 implementation; reviewed 2026-10-08).

This change selectively adapts the generator, structured context, and preview
screening into `communications/recruiter_proposals.py` using the Python
standard library. It does **not** vendor the FastAPI service, Pydantic schemas,
OpenAI SDK, archived legacy Command Center, or deployment artifacts.

## Deliberate limits

* This is a proposal-only adapter, not yet integrated into the live CLI or UI.
* LLM connectivity requires an explicit endpoint, credential, and model; there
  are no automatic cloud calls. Remote HTTP without TLS is refused.
* Recruiter and posting content is untrusted text. Prompt injection is not
  solved by a system prompt; generated wording always needs operator review.
* The heuristic `safe_to_preview` flag is not an authorization decision.
* This module imports no CareerSignal repository, approval, outward-action,
  or Gmail code, and has no persistence or mail-writing operation.
* A future integration must save generated wording as a new review, show the
  exact message to the operator, and approve that **specific digest, recipient,
  subject, mailbox, and provider** before any Gmail draft creation. Never
  rewrite an already approved review or call outward actions from the generator.

## Follow-up integration required

1. Define the mapping from CareerSignal opportunity and source-message records
   into a bounded, explicit proposal packet.
2. Decide whether the standard-library zero-dependency contract permits an
   opt-in external HTTP LLM provider in the CLI. Keep the local UI read-only.
3. Add a storage migration for proposed wording and immutable review versions,
   rather than mutating a currently approved review.
4. Add end-to-end tests for generated-content substitution, recipient movement,
   denial without approval, failure/retry semantics, and no Gmail sends.

The original RecruiterDraft tree should be retained locally until those
integration gates are satisfied. This PR preserves its *reusable capability*,
not a byte-for-byte archival copy of the entire historical application.
