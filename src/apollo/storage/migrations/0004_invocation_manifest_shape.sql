-- An invocation is recorded with its bundle hash and a manifest the tombstone
-- redaction can match (Codex review of PR #8 at d443d18; follows 0003).
--
-- 0003's insert guard skipped the memory check when the manifest was not an
-- array, and accepted a memory entry whose `included` was not a boolean. Either
-- hid the entry from the tombstone's containment match in
-- apollo_memory_tombstone_complete, so the invocation's bundle hash could
-- survive a tombstone. It also accepted an insert with no bundle hash, although
-- a NULL bundle hash is meant to mean redaction and nothing else.
--
-- CREATE OR REPLACE replaces the whole function definition, including its SET
-- clause, so the pinned search_path is restated here and every table stays
-- schema-qualified. The trigger from 0003 already calls this function by name
-- and is unchanged.
--
-- Trigger rules, not CHECK constraints: the manifest is already write-once
-- (invocation_inputs_write_once, 0001), so checking inserts covers every row
-- written from now on, and a CHECK would re-validate rows that databases
-- already hold. No existing row is read or rewritten.

CREATE OR REPLACE FUNCTION apollo_invocation_insert_guard() RETURNS trigger LANGUAGE plpgsql
    SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    entry        jsonb;
    ref          text;
    ref_status   text;
BEGIN
    IF NEW.status <> 'started' OR NEW.rendered_prompt_hash IS NOT NULL
       OR NEW.hashes_redacted_at IS NOT NULL OR NEW.hashes_redacted_reason IS NOT NULL
       OR NEW.context_bundle_hash IS NULL THEN
        RAISE EXCEPTION 'apollo: an invocation is recorded started, before its call, with its '
                        'bundle hash, no prompt hash and no redaction'
            USING ERRCODE = 'restrict_violation';
    END IF;
    -- The manifest is an ordered array of entry objects (spec F.6). Anything
    -- else would hide a memory entry from this check and from the tombstone's
    -- containment match, which is what keeps the redaction complete.
    IF jsonb_typeof(NEW.context_manifest) IS DISTINCT FROM 'array' THEN
        RAISE EXCEPTION 'apollo: a context manifest is an array of entries'
            USING ERRCODE = 'restrict_violation';
    END IF;
    FOR entry IN SELECT value FROM jsonb_array_elements(NEW.context_manifest) LOOP
        IF jsonb_typeof(entry) IS DISTINCT FROM 'object' THEN
            RAISE EXCEPTION 'apollo: a context manifest entry is an object'
                USING ERRCODE = 'restrict_violation';
        END IF;
        CONTINUE WHEN entry ->> 'source_kind' IS DISTINCT FROM 'memory';
        -- A memory entry is in exactly the form the redaction query matches.
        ref := entry ->> 'source_ref';
        IF jsonb_typeof(entry -> 'included') IS DISTINCT FROM 'boolean'
           OR ref IS NULL
           OR ref !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
            RAISE EXCEPTION 'apollo: a memory manifest entry names its memory by canonical uuid '
                            'and says whether it was included'
                USING ERRCODE = 'restrict_violation';
        END IF;
        CONTINUE WHEN entry -> 'included' = 'false'::jsonb;
        SELECT status INTO ref_status FROM public.memory WHERE id = ref::uuid FOR SHARE;
        IF ref_status IS NULL OR ref_status = 'tombstoned' THEN
            RAISE EXCEPTION 'apollo: an invocation cannot include a tombstoned or missing memory'
                USING ERRCODE = 'restrict_violation';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$;
