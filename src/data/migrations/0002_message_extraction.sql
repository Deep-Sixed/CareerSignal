CREATE TABLE message_sources (
 message_id TEXT PRIMARY KEY REFERENCES messages(id),
 namespace TEXT NOT NULL, external_id TEXT NOT NULL,
 sender TEXT NOT NULL, subject TEXT NOT NULL,
 format TEXT NOT NULL CHECK(format IN ('text','html')),
 parser_version TEXT NOT NULL
);
-- statement
CREATE TABLE extraction_items (
 message_id TEXT NOT NULL REFERENCES messages(id),
 item_index INTEGER NOT NULL CHECK(item_index >= 0),
 excerpt TEXT NOT NULL, reason TEXT NOT NULL,
 opportunity_id TEXT REFERENCES opportunities(id),
 review_id TEXT REFERENCES reviews(id),
 PRIMARY KEY(message_id, item_index),
 CHECK((opportunity_id IS NULL AND review_id IS NULL AND length(reason)>0)
 OR (opportunity_id IS NOT NULL AND review_id IS NOT NULL AND reason=''))
);
