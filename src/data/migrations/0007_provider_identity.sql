-- One review still permits at most one external draft attempt. Before this migration that
-- attempt had no durable destination: an approval recorded content, wording, status and
-- addressing, but not which provider or which mailbox it authorized. A controlled approval
-- and a Gmail request to any mailbox were indistinguishable to claim(), and a Gmail approval
-- for one mailbox would silently authorize a draft to another.
--
-- Two columns bind an approval, and later the intent it produces, to a destination:
--   provider            which implementation owns the external action ("controlled", "gmail")
--   provider_namespace  which destination owns it (a stable controlled namespace, or
--                        "gmail:operator@example.com")
--
-- Deliberately not a uniqueness key. draft_intents stays globally one-attempt-per-review;
-- these columns describe that one attempt, they do not create a slot per provider or per
-- mailbox. A review approved for one destination and then requested against another is a
-- mismatch to refuse, never a second attempt to reserve.
--
-- Rows written before this migration carry '', exactly as migrations 0005 and 0006 left
-- their own columns for the same reason: nothing recorded which provider or mailbox those
-- approvals or attempts were for, so claiming they bind to "controlled" or to any mailbox
-- would be an invention. An old approval no longer authorizes an outward draft; an old
-- intent never authorizes another write. The operator re-approves, having named the
-- destination the current contract requires.
ALTER TABLE decisions ADD COLUMN provider TEXT NOT NULL DEFAULT '';
-- statement
ALTER TABLE decisions ADD COLUMN provider_namespace TEXT NOT NULL DEFAULT '';
-- statement
-- The provider identity the authorization check actually verified, recorded on the intent
-- alongside the status event and the source, so the record says not only what was sent and
-- to whom, but through which destination.
ALTER TABLE draft_intents ADD COLUMN provider TEXT NOT NULL DEFAULT '';
-- statement
ALTER TABLE draft_intents ADD COLUMN provider_namespace TEXT NOT NULL DEFAULT '';
