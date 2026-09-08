-- Scoring v2 records the components of stated skill coverage. coverage is NULL exactly when
-- the job stated no skills, which is unscored rather than a perfect or zero fit. The legacy
-- reviews.score column is NOT NULL and cannot be relaxed while foreign keys are enforced
-- (rebuilding reviews would break the provenance and decisions references), so it mirrors
-- coverage and holds 0 for an unscored review; coverage is the authoritative value.
ALTER TABLE reviews ADD COLUMN stated_skills INTEGER;
-- statement
ALTER TABLE reviews ADD COLUMN matched_skills INTEGER;
-- statement
ALTER TABLE reviews ADD COLUMN coverage INTEGER;
