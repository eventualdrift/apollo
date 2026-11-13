-- Memory proposals, held by the database (spec C.9, D.3).
--
-- The rules live in `apollo.memory.proposals`; this migration makes the
-- database refuse anything outside them. No table or column is added. No
-- deployment holds a proposal row yet: nothing created one before step 12.
--
-- As in 0003, rules on this content-bearing table are triggers rather than
-- CHECK constraints wherever a choice exists: a CHECK violation reports
-- "Failing row contains (...)", and a pending proposal's row contains its
-- text. Every message raised below names a rule, never a value. The one CHECK
-- added is the closed enum on `kind`, which the old repository review asked
-- for as a named constraint (finding 15); the row it would quote has already
-- failed `apollo.memory.proposals`' own validation of the same enum.
--
-- Every function pins `search_path = pg_catalog, public, pg_temp` and
-- schema-qualifies the tables it reads, for the reason given in 0003.

-- `kind` is a closed enum, as `scope` and `status` already are (spec C.9).
ALTER TABLE memory_proposal ADD CONSTRAINT proposal_kind_ck CHECK (kind IN (
    'fact', 'preference', 'person', 'project', 'decision', 'constraint', 'event'));

-- At most one proposal per turn: no batching, no multi-memory extraction
-- (spec D.3/6). One invocation structures one proposal, and one proposal
-- creates at most one memory.
ALTER TABLE memory_proposal ADD CONSTRAINT proposal_turn_uq UNIQUE (turn_id);
ALTER TABLE memory_proposal ADD CONSTRAINT proposal_invocation_uq UNIQUE (model_invocation_id);
ALTER TABLE memory_proposal ADD CONSTRAINT proposal_memory_uq UNIQUE (resulting_memory_id);

CREATE FUNCTION apollo_proposal_guard() RETURNS trigger LANGUAGE plpgsql
    SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
    inv   record;
    turn  record;
    made  record;
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- A proposal is created pending, by the structuring invocation of the
        -- turn whose request message it came from (spec D.3/3: exact provenance).
        IF NEW.status <> 'pending' OR NEW.resolved_at IS NOT NULL
           OR NEW.resulting_memory_id IS NOT NULL OR NEW.subject IS NULL THEN
            RAISE EXCEPTION 'apollo: a proposal is created pending, with its text and no resolution'
                USING ERRCODE = 'restrict_violation';
        END IF;
        -- Self-model mutation is direct entry only (spec D.3/5).
        IF NEW.scope = 'self' THEN
            RAISE EXCEPTION 'apollo: a proposal cannot have scope self'
                USING ERRCODE = 'restrict_violation';
        END IF;
        -- The invocation is completed in this same transaction, just before
        -- (spec A.2, Tp): a failed or unfinished generation proposes nothing.
        SELECT purpose, turn_id, status INTO inv
          FROM public.model_invocation WHERE id = NEW.model_invocation_id;
        IF inv.purpose IS DISTINCT FROM 'memory_proposal'
           OR inv.turn_id IS DISTINCT FROM NEW.turn_id
           OR inv.status IS DISTINCT FROM 'completed' THEN
            RAISE EXCEPTION 'apollo: a proposal names the completed memory_proposal invocation '
                            'of its own turn'
                USING ERRCODE = 'restrict_violation';
        END IF;
        SELECT conversation_id, request_message_id INTO turn
          FROM public.turn WHERE id = NEW.turn_id;
        IF turn.conversation_id IS DISTINCT FROM NEW.conversation_id
           OR turn.request_message_id IS DISTINCT FROM NEW.source_message_id THEN
            RAISE EXCEPTION 'apollo: a proposal''s source is the request message of its own turn'
                USING ERRCODE = 'restrict_violation';
        END IF;
        -- A claim Janu has forgotten is not offered back from the same message
        -- (spec D.7). FOR SHARE serialises this with a concurrent tombstone of a
        -- memory citing that message: if the tombstone commits first this insert
        -- is refused; if this insert commits first, the tombstone declines it.
        PERFORM 1 FROM public.memory m
          WHERE m.id IN (SELECT o.memory_id FROM public.memory_observation o
                          WHERE o.message_id = NEW.source_message_id)
            FOR SHARE OF m;
        IF EXISTS (SELECT 1 FROM public.memory m
                    JOIN public.memory_observation o ON o.memory_id = m.id
                   WHERE o.message_id = NEW.source_message_id AND m.status = 'tombstoned') THEN
            RAISE EXCEPTION 'apollo: no proposal is made from a message a tombstoned memory cites'
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END IF;

    -- What a proposal is about, and where it came from, never moves.
    IF NEW.conversation_id IS DISTINCT FROM OLD.conversation_id
       OR NEW.turn_id IS DISTINCT FROM OLD.turn_id
       OR NEW.model_invocation_id IS DISTINCT FROM OLD.model_invocation_id
       OR NEW.source_message_id IS DISTINCT FROM OLD.source_message_id
       OR NEW.scope IS DISTINCT FROM OLD.scope
       OR NEW.kind IS DISTINCT FROM OLD.kind
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'apollo: a proposal''s references, scope, kind and creation time are write-once'
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- The only change a proposal undergoes is its resolution, once (spec C.9):
    -- pending -> saved | saved_edited | ignored | expired. Nothing is revived.
    IF OLD.status <> 'pending' OR NEW.status = 'pending' THEN
        RAISE EXCEPTION 'apollo: a proposal is resolved once, from pending, and never changes again'
            USING ERRCODE = 'restrict_violation';
    END IF;
    IF NEW.resolved_at IS NULL THEN
        RAISE EXCEPTION 'apollo: a resolved proposal records when it was resolved'
            USING ERRCODE = 'restrict_violation';
    END IF;

    IF NEW.status IN ('saved', 'saved_edited') THEN
        -- Saving creates the memory in the same transaction. It is a
        -- user-asserted, active, non-self memory whose `asserts` observation
        -- says a model structured it and the user authorised it, and points at
        -- the originating message (spec D.3/3, D.3/4, D.3/5).
        SELECT scope, status, origin_tier, origin INTO made
          FROM public.memory WHERE id = NEW.resulting_memory_id;
        IF made.status IS DISTINCT FROM 'active'
           OR made.origin_tier IS DISTINCT FROM 'user_asserted'
           OR made.origin IS DISTINCT FROM 'personal'
           OR made.scope IS NOT DISTINCT FROM 'self' THEN
            RAISE EXCEPTION 'apollo: a saved proposal names the active, user-asserted, '
                            'non-self memory it created'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM public.memory_observation
                        WHERE memory_id = NEW.resulting_memory_id
                          AND relation = 'asserts'
                          AND source_kind = 'model_proposal_approved'
                          AND message_id = NEW.source_message_id) THEN
            RAISE EXCEPTION 'apollo: a saved proposal''s memory is asserted by its source message, '
                            'as an approved model proposal'
                USING ERRCODE = 'restrict_violation';
        END IF;
    ELSIF NEW.resulting_memory_id IS NOT NULL THEN
        RAISE EXCEPTION 'apollo: only a saved proposal names a memory'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER proposal_guard BEFORE INSERT OR UPDATE ON memory_proposal
    FOR EACH ROW EXECUTE FUNCTION apollo_proposal_guard();

-- The reverse direction: a memory whose assertion says "approved model
-- proposal" really was approved through one, so nothing can create a memory
-- from model output without the proposal a person saved (spec D.3/2). Deferred
-- to commit, because the memory and its observation are written before the
-- proposal is resolved.
CREATE FUNCTION apollo_approved_proposal_exists() RETURNS trigger LANGUAGE plpgsql
    SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF NEW.relation = 'asserts' AND NEW.source_kind = 'model_proposal_approved' AND NOT EXISTS (
        SELECT 1 FROM public.memory_proposal
         WHERE resulting_memory_id = NEW.memory_id
           AND source_message_id = NEW.message_id
           AND status IN ('saved', 'saved_edited')) THEN
        RAISE EXCEPTION 'apollo: an approved-proposal observation needs the proposal that was saved'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER observation_approved_proposal_exists AFTER INSERT ON memory_observation
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION apollo_approved_proposal_exists();

-- A tombstone is complete by commit (spec D.7/3, as 0003 holds for excerpts and
-- hashes): no proposal made from a message the memory cites is left pending.
-- Such a proposal is Apollo's own wording of the claim being forgotten.
CREATE FUNCTION apollo_tombstone_declines_proposals() RETURNS trigger LANGUAGE plpgsql
    SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM public.memory_proposal p
                JOIN public.memory_observation o ON o.message_id = p.source_message_id
               WHERE o.memory_id = NEW.id AND p.status = 'pending') THEN
        RAISE EXCEPTION 'apollo: a tombstone declines every pending proposal made from a '
                        'message its memory cites'
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER memory_tombstone_declines_proposals AFTER UPDATE ON memory
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    WHEN (NEW.status = 'tombstoned' AND OLD.status IS DISTINCT FROM 'tombstoned')
    EXECUTE FUNCTION apollo_tombstone_declines_proposals();

CREATE INDEX proposal_conversation_ix ON memory_proposal (conversation_id, created_at);
CREATE INDEX proposal_source_message_ix ON memory_proposal (source_message_id);
-- Both rules above look a message's observations up by the message.
CREATE INDEX observation_message_ix ON memory_observation (message_id)
    WHERE message_id IS NOT NULL;
