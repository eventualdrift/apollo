-- The memory lifecycle, held by the database (spec D.1 as clarified by D.9).
--
-- The rules live in `apollo.memory.lifecycle` as data; this migration makes the
-- database refuse anything outside them, so a bug or a hand-written UPDATE
-- cannot produce a state the lifecycle forbids. No table or column is added
-- and no existing row is rewritten.
--
-- Rules on the content-bearing tables (`memory`, `memory_observation`) are
-- triggers rather than CHECK constraints wherever a choice exists: a CHECK
-- violation reports "Failing row contains (...)", which would put claim text
-- into error messages and the server log. The one CHECK added to `memory` is
-- the phase-zero origin_tier rule, which Janu approved as a named constraint so
-- a later phase can drop it. Every message raised below names statuses and
-- rules, never a value.
--
-- Every function below pins `search_path = pg_catalog, public, pg_temp` and
-- schema-qualifies the tables it reads. Unqualified, a lookup would resolve a
-- session's temporary table first (pg_temp is searched before public unless
-- listed), and the runtime role holds TEMP by default: a temporary `memory`
-- table could otherwise vouch for a row the real table says is tombstoned.

-- ---------------------------------------------------------------------------
-- memory
-- ---------------------------------------------------------------------------

-- Phase zero writes only user_asserted (spec C.7, ADR-0005). The enum in 0001
-- keeps all three values for later phases; this narrower rule is dropped then.
ALTER TABLE memory ADD CONSTRAINT memory_phase0_origin_tier_ck
    CHECK (origin_tier = 'user_asserted');

-- A row replaces at most one row, so a supersession chain cannot branch or merge.
ALTER TABLE memory ADD CONSTRAINT memory_superseded_by_uq UNIQUE (superseded_by_id);

CREATE FUNCTION apollo_memory_lifecycle_guard() RETURNS trigger LANGUAGE plpgsql
    SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    succ_status text;
    succ_next   uuid;
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- Every row is created active: a direct entry, an approved proposal, or
        -- the replacement row of a correction.
        IF NEW.status <> 'active' OR NEW.superseded_by_id IS NOT NULL
           OR NEW.archived_at IS NOT NULL OR NEW.tombstoned_at IS NOT NULL THEN
            RAISE EXCEPTION 'apollo: memory rows are created active, with no successor '
                            'and no archive or tombstone time'
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;

    -- Tombstoned is terminal: nothing about the row changes again.
    IF OLD.status = 'tombstoned'
       AND (to_jsonb(NEW) - 'search_vector') IS DISTINCT FROM (to_jsonb(OLD) - 'search_vector') THEN
        RAISE EXCEPTION 'apollo: a tombstoned memory is terminal'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- The successor pointer is write-once.
    IF OLD.superseded_by_id IS NOT NULL
       AND NEW.superseded_by_id IS DISTINCT FROM OLD.superseded_by_id THEN
        RAISE EXCEPTION 'apollo: memory.superseded_by_id is write-once'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- A successor is set only by a correction, never alongside another change.
    IF OLD.superseded_by_id IS NULL AND NEW.superseded_by_id IS NOT NULL
       AND NOT (OLD.status = 'active' AND NEW.status = 'superseded') THEN
        RAISE EXCEPTION 'apollo: memory.superseded_by_id is set only by a correction'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- Exactly the row transitions in apollo.memory.lifecycle.ROW_TRANSITIONS.
    IF NEW.status IS DISTINCT FROM OLD.status
       AND (OLD.status, NEW.status) NOT IN (
            ('active', 'superseded'),
            ('active', 'archived'),
            ('archived', 'active'),
            ('active', 'tombstoned'),
            ('archived', 'tombstoned'),
            ('superseded', 'tombstoned')) THEN
        RAISE EXCEPTION 'apollo: memory status % -> % is not a lifecycle transition',
                        OLD.status, NEW.status
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- Each status has one shape.
    IF (NEW.status = 'active' AND (NEW.superseded_by_id IS NOT NULL
            OR NEW.archived_at IS NOT NULL OR NEW.tombstoned_at IS NOT NULL))
       OR (NEW.status = 'archived' AND (NEW.archived_at IS NULL
            OR NEW.superseded_by_id IS NOT NULL OR NEW.tombstoned_at IS NOT NULL))
       OR (NEW.status = 'superseded' AND (NEW.superseded_by_id IS NULL
            OR NEW.tombstoned_at IS NOT NULL))
       OR (NEW.status = 'tombstoned' AND NEW.tombstoned_at IS NULL) THEN
        RAISE EXCEPTION 'apollo: a % memory has the wrong successor, archive or tombstone fields',
                        NEW.status
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- Correct: the replacement must be a live chain head other than this row.
    -- Pointing only at an active head is also what rules out cycles. FOR SHARE
    -- serialises this with a concurrent tombstone of that head: whichever
    -- commits second sees the other and is refused.
    IF OLD.status = 'active' AND NEW.status = 'superseded' THEN
        SELECT status, superseded_by_id INTO succ_status, succ_next
          FROM public.memory WHERE id = NEW.superseded_by_id FOR SHARE;
        IF NEW.superseded_by_id = NEW.id OR succ_status IS DISTINCT FROM 'active'
           OR succ_next IS NOT NULL THEN
            RAISE EXCEPTION 'apollo: a memory is superseded only by an active chain head '
                            'other than itself'
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;

    -- A superseded row is tombstoned only with its chain, after its successor
    -- (spec D.9/1). The deferred check below makes the whole chain go together.
    IF OLD.status = 'superseded' AND NEW.status = 'tombstoned' THEN
        SELECT status INTO succ_status FROM public.memory WHERE id = NEW.superseded_by_id FOR SHARE;
        IF succ_status IS DISTINCT FROM 'tombstoned' THEN
            RAISE EXCEPTION 'apollo: a superseded memory is tombstoned only with its chain, '
                            'after its successor'
                USING ERRCODE = 'restrict_violation';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER memory_lifecycle_guard BEFORE INSERT OR UPDATE ON memory
    FOR EACH ROW EXECUTE FUNCTION apollo_memory_lifecycle_guard();

-- Provenance required (spec D.1): at commit, every new memory has an `asserts`
-- observation. Deferred, because the observation references the memory and so
-- can only be inserted after it, in the same transaction.
CREATE FUNCTION apollo_memory_requires_assertion() RETURNS trigger LANGUAGE plpgsql
    SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM public.memory_observation
                    WHERE memory_id = NEW.id AND relation = 'asserts') THEN
        RAISE EXCEPTION 'apollo: a memory cannot exist without an asserts observation'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER memory_requires_assertion AFTER INSERT ON memory
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION apollo_memory_requires_assertion();

-- A tombstone is complete by commit (spec D.7, D.9/1): the row's observation
-- excerpts are cleared, and no predecessor in its chain is left untombstoned.
-- Applied at each link, the second rule covers the whole chain.
CREATE FUNCTION apollo_memory_tombstone_complete() RETURNS trigger LANGUAGE plpgsql
    SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.memory_observation
                WHERE memory_id = NEW.id AND excerpt IS NOT NULL) THEN
        RAISE EXCEPTION 'apollo: a tombstone clears every observation excerpt of its memory'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF EXISTS (SELECT 1 FROM public.memory
                WHERE superseded_by_id = NEW.id AND status <> 'tombstoned') THEN
        RAISE EXCEPTION 'apollo: a memory is tombstoned together with every predecessor in its chain'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER memory_tombstone_complete AFTER UPDATE ON memory
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    WHEN (NEW.status = 'tombstoned' AND OLD.status IS DISTINCT FROM 'tombstoned')
    EXECUTE FUNCTION apollo_memory_tombstone_complete();

-- ---------------------------------------------------------------------------
-- memory_observation
-- ---------------------------------------------------------------------------

CREATE FUNCTION apollo_observation_guard() RETURNS trigger LANGUAGE plpgsql
    SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    parent_status text;
BEGIN
    -- FOR SHARE serialises this with a concurrent tombstone of the parent: the
    -- foreign key's own KEY SHARE lock does not conflict with an UPDATE of
    -- non-key columns, so without it both could commit.
    SELECT status INTO parent_status FROM public.memory WHERE id = NEW.memory_id FOR SHARE;

    IF TG_OP = 'INSERT' THEN
        IF parent_status = 'tombstoned' THEN
            RAISE EXCEPTION 'apollo: no observation may be added to a tombstoned memory'
                USING ERRCODE = 'restrict_violation';
        END IF;
        -- An assertion is made when the claim is created, and it is created active.
        IF NEW.relation = 'asserts' AND parent_status IS DISTINCT FROM 'active' THEN
            RAISE EXCEPTION 'apollo: an asserts observation belongs to an active memory'
                USING ERRCODE = 'restrict_violation';
        END IF;
        -- Confirm and contradict apply to active and archived rows only (step 10 plan,
        -- decision 5).
        IF NEW.relation IN ('confirms', 'contradicts')
           AND parent_status NOT IN ('active', 'archived') THEN
            RAISE EXCEPTION 'apollo: % applies to an active or archived memory, not a % one',
                            NEW.relation, parent_status
                USING ERRCODE = 'restrict_violation';
        END IF;
        -- A conversational source names its message; a direct entry has none.
        IF (NEW.source_kind IN ('user_message', 'model_proposal_approved'))
           <> (NEW.message_id IS NOT NULL) THEN
            RAISE EXCEPTION 'apollo: a % observation % a message_id', NEW.source_kind,
                CASE WHEN NEW.source_kind IN ('user_message', 'model_proposal_approved')
                     THEN 'requires' ELSE 'must not have' END
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;

    -- Observations are evidence: write-once, except that tombstoning clears the
    -- excerpt (spec D.7), and only then.
    IF (to_jsonb(NEW) - 'excerpt') IS DISTINCT FROM (to_jsonb(OLD) - 'excerpt') THEN
        RAISE EXCEPTION 'apollo: memory_observation rows are write-once'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.excerpt IS DISTINCT FROM OLD.excerpt
       AND (NEW.excerpt IS NOT NULL OR parent_status IS DISTINCT FROM 'tombstoned') THEN
        RAISE EXCEPTION 'apollo: an excerpt is only ever cleared, and only once its memory '
                        'is tombstoned'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER observation_guard BEFORE INSERT OR UPDATE ON memory_observation
    FOR EACH ROW EXECUTE FUNCTION apollo_observation_guard();

-- ---------------------------------------------------------------------------
-- model_invocation: verification-hash redaction is one-way (spec D.7)
-- ---------------------------------------------------------------------------

-- No content lives on this table, so a CHECK is safe here.
ALTER TABLE model_invocation ADD CONSTRAINT invocation_redaction_ck CHECK (
    (hashes_redacted_at IS NULL) = (hashes_redacted_reason IS NULL)
    AND (hashes_redacted_reason IS NULL OR hashes_redacted_reason = 'source_tombstoned')
    AND (hashes_redacted_at IS NULL
         OR (context_bundle_hash IS NULL AND rendered_prompt_hash IS NULL))
);

CREATE FUNCTION apollo_invocation_hash_guard() RETURNS trigger LANGUAGE plpgsql
    SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    redacting boolean := OLD.hashes_redacted_at IS NULL AND NEW.hashes_redacted_at IS NOT NULL;
BEGIN
    -- Once redacted, always redacted: the hashes stay null and the record of
    -- the redaction does not move.
    IF OLD.hashes_redacted_at IS NOT NULL
       AND (NEW.hashes_redacted_at IS DISTINCT FROM OLD.hashes_redacted_at
            OR NEW.hashes_redacted_reason IS DISTINCT FROM OLD.hashes_redacted_reason) THEN
        RAISE EXCEPTION 'apollo: a hash redaction is permanent'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- The bundle hash is recorded before the call and changes only by redaction.
    IF NEW.context_bundle_hash IS DISTINCT FROM OLD.context_bundle_hash AND NOT redacting THEN
        RAISE EXCEPTION 'apollo: context_bundle_hash changes only by tombstone redaction'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- The prompt hash is recorded once, by the same update that completes a
    -- started call, and otherwise changes only by redaction. Accepting it on a
    -- row that stays `started` would let a stray write pre-empt the real hash.
    IF NEW.rendered_prompt_hash IS DISTINCT FROM OLD.rendered_prompt_hash
       AND NOT redacting
       AND NOT (OLD.rendered_prompt_hash IS NULL AND OLD.status = 'started'
                AND NEW.status = 'completed') THEN
        RAISE EXCEPTION 'apollo: rendered_prompt_hash is set once at completion and '
                        'otherwise changes only by tombstone redaction'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER invocation_hash_guard BEFORE UPDATE ON model_invocation
    FOR EACH ROW EXECUTE FUNCTION apollo_invocation_hash_guard();
