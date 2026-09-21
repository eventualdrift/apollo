-- The tombstone must not be a hole in the write-once guarantee.
--
-- 0001 attached one trigger to `memory` with WHEN (NEW.status <> 'tombstoned').
-- That exempts the *entire* UPDATE whenever a row is being tombstoned, so a
-- single statement could tombstone a claim and rewrite its provenance at the
-- same time — changing `origin_tier` from `user_asserted` to `model_inferred`,
-- for instance, which is precisely what the frozen provenance model (spec C.7,
-- ADR-0005) says can never happen.
--
-- Split by responsibility instead of by transition:
--
--   * classification and provenance are write-once in EVERY transition,
--     including the tombstone;
--   * only the claim text may be cleared, and only while tombstoning.
--
-- Nothing about the memory lifecycle changes, and no existing row is rewritten.
-- `memory_content_presence_ck` from 0001 continues to force a tombstoned row's
-- subject and content to NULL, so the second trigger's exemption permits
-- clearing them and nothing else: an UPDATE that tombstoned a row while writing
-- new text would fail the CHECK, and one that later revived the row with text
-- would trip the trigger, which keeps the tombstone terminal.

DROP TRIGGER memory_claim_write_once ON memory;

-- Write-once in every transition, tombstone included.
CREATE TRIGGER memory_provenance_write_once BEFORE UPDATE ON memory
    FOR EACH ROW EXECUTE FUNCTION apollo_reject_column_change(
        'scope', 'kind', 'origin_tier', 'origin', 'created_at');

-- Write-once except while tombstoning, which may only clear them (see CHECK).
CREATE TRIGGER memory_claim_text_write_once BEFORE UPDATE ON memory
    FOR EACH ROW WHEN (NEW.status <> 'tombstoned')
    EXECUTE FUNCTION apollo_reject_column_change('subject', 'content');
