CREATE TABLE messages (id TEXT PRIMARY KEY, digest TEXT NOT NULL);
-- statement
CREATE TABLE opportunities (id TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE,
 title TEXT NOT NULL, company TEXT NOT NULL, location TEXT NOT NULL,
 current_review TEXT REFERENCES reviews(id) DEFERRABLE INITIALLY DEFERRED);
-- statement
CREATE TABLE reviews (id TEXT PRIMARY KEY, opportunity_id TEXT NOT NULL REFERENCES opportunities(id),
 content_digest TEXT NOT NULL,
 score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 100),
 advances INTEGER NOT NULL CHECK(advances IN (0,1)), payload TEXT NOT NULL CHECK(json_valid(payload)),
 draft TEXT NOT NULL);
-- statement
CREATE TABLE provenance (message_id TEXT NOT NULL REFERENCES messages(id),
 opportunity_id TEXT NOT NULL REFERENCES opportunities(id), review_id TEXT NOT NULL REFERENCES reviews(id),
 PRIMARY KEY(message_id, opportunity_id));
-- statement
CREATE TABLE decisions (review_id TEXT PRIMARY KEY REFERENCES reviews(id),
 approved INTEGER NOT NULL CHECK(approved IN (0,1)), actor TEXT NOT NULL CHECK(length(actor)>0));
-- statement
CREATE TABLE draft_intents (review_id TEXT PRIMARY KEY REFERENCES decisions(review_id),
 state TEXT NOT NULL CHECK(state IN ('attempting','uncertain','confirmed')), receipt TEXT UNIQUE,
 CHECK((state='confirmed' AND receipt IS NOT NULL) OR (state!='confirmed' AND receipt IS NULL)));
-- statement
CREATE TABLE audit (id INTEGER PRIMARY KEY, review_id TEXT REFERENCES reviews(id),
 event TEXT NOT NULL, created_at INTEGER NOT NULL DEFAULT (unixepoch()));
-- statement
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
-- statement
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT, 'audit is append-only'); END;
