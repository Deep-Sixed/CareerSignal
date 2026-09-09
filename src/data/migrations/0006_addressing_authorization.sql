-- An approval authorizes an outward action, and an outward action has a target. Migration
-- 0005 bound an approval to what the review said; it did not bind who the message would be
-- addressed to, and those are not the same question.
--
-- The gap was reachable. An opportunity can arrive in more than one message, replay reuses
-- the review when the job has not changed, and the newest source addresses the draft. So a
-- second message about the same job from a different sender moved the target of an already
-- approved draft, while every bound value stayed identical:
--
--   approve while the source is jane@example.com
--   a later message about the same job arrives from bob@example.com
--   content_digest, draft_digest and status are all unchanged
--   the draft is created, addressed to bob@example.com, with no second approval
--
-- addressing_digest closes it: the approval covers the sender and subject that were current
-- when the operator read it. A newer source that changes either is a materially different
-- outward action and needs a new decision, which is not the same as discarding the old one.
-- source_message_id is provenance rather than authorization: it records which message the
-- operator was looking at, so a later disagreement can be traced to a specific message.
--
-- Rows written before this migration carry '' and cannot match, exactly as in 0005, and for
-- the same reason: nothing records which source those approvals were for.
ALTER TABLE decisions ADD COLUMN addressing_digest TEXT NOT NULL DEFAULT '';
-- statement
ALTER TABLE decisions ADD COLUMN source_message_id TEXT REFERENCES messages(id);
-- statement
-- The source the authorization check actually observed, recorded on the intent alongside
-- the status event, so the record says what was sent and to whom it was addressed.
ALTER TABLE draft_intents ADD COLUMN source_message_id TEXT REFERENCES messages(id);
