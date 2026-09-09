-- Status history is append-only, like audit. The current status is the latest row rather
-- than a stored column, so a correction is a new event and how the operator arrived at a
-- state is never lost. The id is a plain rowid: nothing is ever deleted from this table, so
-- ids stay monotonic and "latest" is unambiguous even when two events share a timestamp.
--
-- The status vocabulary is repeated here because the database should reject a bad value on
-- its own. That makes it a second source of truth, so a test asserts this list and
-- recruiting.status.STATUSES stay equal.
CREATE TABLE opportunity_status_history (
 id INTEGER PRIMARY KEY,
 opportunity_id TEXT NOT NULL REFERENCES opportunities(id),
 status TEXT NOT NULL CHECK(status IN ('new','reviewing','interested','applied','interviewing','offer','rejected','withdrawn','closed')),
 actor TEXT NOT NULL CHECK(length(actor)>0),
 reason TEXT NOT NULL DEFAULT '',
 created_at INTEGER NOT NULL DEFAULT (unixepoch()));
-- statement
CREATE INDEX opportunity_status_history_current ON opportunity_status_history(opportunity_id, id);
-- statement
CREATE TRIGGER opportunity_status_history_no_update BEFORE UPDATE ON opportunity_status_history BEGIN SELECT RAISE(ABORT, 'status history is append-only'); END;
-- statement
CREATE TRIGGER opportunity_status_history_no_delete BEFORE DELETE ON opportunity_status_history BEGIN SELECT RAISE(ABORT, 'status history is append-only'); END;
-- statement
-- Opportunities recorded before this migration have no history. Give them the same start
-- every new opportunity gets, and say in the row itself that it was backfilled rather than
-- observed, so the record does not claim an operator was there.
INSERT INTO opportunity_status_history(opportunity_id,status,actor,reason)
 SELECT id, 'new', 'migration', 'Backfilled when status history was introduced' FROM opportunities;
