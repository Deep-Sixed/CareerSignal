-- An approval authorizes one external write, so it has to say exactly what was approved.
-- Before this migration a decision recorded only "approved" and by whom, which is enough
-- to authorize a local action and not enough to authorize creating content in somebody
-- else's mailbox: the review could be rescored, its wording changed, or the opportunity
-- terminated between the operator reading it and the draft being created.
--
-- The three columns bind an approval to the thing that was read:
--   content_digest    the review's own digest of the opportunity it describes
--   draft_digest      the exact draft wording the operator approved
--   status_event_id   the status history event that was current when they approved
--
-- Rows written before this migration carry '' and 0. That is not a value that can match,
-- so an approval recorded under the old contract no longer authorizes a draft. This is
-- deliberate and is not backfilled: nothing in the database records what those approvals
-- were for, so claiming they bind to the current review would be an invention. The
-- operator re-approves, having read what is there now.
ALTER TABLE decisions ADD COLUMN content_digest TEXT NOT NULL DEFAULT '';
-- statement
ALTER TABLE decisions ADD COLUMN draft_digest TEXT NOT NULL DEFAULT '';
-- statement
ALTER TABLE decisions ADD COLUMN status_event_id INTEGER NOT NULL DEFAULT 0;
-- statement
-- The status event the authorization check actually observed, recorded on the intent
-- rather than inferred later. An event appended after this one is not a fault: it says
-- the draft was authorized against a state the operator can now see was superseded.
ALTER TABLE draft_intents ADD COLUMN status_event_id INTEGER NOT NULL DEFAULT 0;
