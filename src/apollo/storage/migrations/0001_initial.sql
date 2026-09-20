-- Apollo phase zero, the eleven-table schema (spec C).
--
-- Write-once columns are enforced by triggers rather than by convention:
-- reconstruction (spec H.4) depends on claim content and message content being
-- write-once, so a mechanism is worth more here than a comment.

CREATE TABLE conversation (
    id              uuid PRIMARY KEY,
    title           text,
    mode            text        NOT NULL DEFAULT 'personal',
    started_at      timestamptz NOT NULL,
    last_active_at  timestamptz NOT NULL,
    archived_at     timestamptz,
    CONSTRAINT conversation_mode_ck CHECK (mode IN ('personal', 'benchmark'))
);

CREATE TABLE message (
    id                     uuid PRIMARY KEY,
    conversation_id        uuid   NOT NULL REFERENCES conversation(id),
    seq                    bigint NOT NULL,
    role                   text   NOT NULL,
    content                text   NOT NULL,
    created_at             timestamptz NOT NULL,
    device_id              text,
    turn_id                uuid,
    client_idempotency_key text,
    truncated              boolean NOT NULL DEFAULT false,
    CONSTRAINT message_role_ck CHECK (role IN ('user', 'apollo', 'system_note')),
    CONSTRAINT message_seq_uq  UNIQUE (conversation_id, seq),
    CONSTRAINT message_idem_uq UNIQUE (conversation_id, client_idempotency_key)
);
CREATE INDEX message_conversation_seq_ix ON message (conversation_id, seq);

CREATE TABLE turn (
    id                  uuid PRIMARY KEY,
    conversation_id     uuid NOT NULL REFERENCES conversation(id),
    request_message_id  uuid NOT NULL REFERENCES message(id),
    response_message_id uuid REFERENCES message(id),
    status              text NOT NULL,
    conversation_mode   text NOT NULL,
    identity_version    text NOT NULL,
    identity_hash       text NOT NULL,
    started_at          timestamptz NOT NULL,
    completed_at        timestamptz,
    latency_ms          integer,
    error_kind          text,
    error_detail        text,
    CONSTRAINT turn_status_ck CHECK (status IN ('started', 'completed', 'failed')),
    CONSTRAINT turn_mode_ck   CHECK (conversation_mode IN ('personal', 'benchmark')),
    CONSTRAINT turn_detail_len_ck CHECK (error_detail IS NULL OR length(error_detail) <= 500)
);
CREATE INDEX turn_conversation_ix ON turn (conversation_id, started_at);
CREATE INDEX turn_status_ix ON turn (status) WHERE status = 'started';

CREATE TABLE model_invocation (
    id                     uuid PRIMARY KEY,
    turn_id                uuid    NOT NULL REFERENCES turn(id),
    seq                    integer NOT NULL,
    purpose                text    NOT NULL,
    retry_of_invocation_id uuid REFERENCES model_invocation(id),
    brain_alias            text    NOT NULL,
    provider_key           text    NOT NULL,
    model_identifier       text,
    adapter_key            text    NOT NULL,
    render_version         text    NOT NULL,
    compiler_version       text    NOT NULL,
    token_estimator        text    NOT NULL,
    context_manifest       jsonb   NOT NULL,
    context_bundle_hash    text,
    context_token_estimate integer NOT NULL,
    max_trust_tier         text    NOT NULL,
    taint                  smallint NOT NULL DEFAULT 0,
    generation_params      jsonb   NOT NULL,
    rendered_prompt_hash   text,
    hashes_redacted_at     timestamptz,
    hashes_redacted_reason text,
    prompt_tokens          integer,
    completion_tokens      integer,
    reasoning_tokens       integer,
    finish_reason          text,
    status                 text    NOT NULL,
    started_at             timestamptz NOT NULL,
    completed_at           timestamptz,
    latency_ms             integer,
    error_kind             text,
    error_detail           text,
    CONSTRAINT invocation_seq_uq     UNIQUE (turn_id, seq),
    -- purpose is a closed enum; adding a value requires a specification change.
    CONSTRAINT invocation_purpose_ck CHECK (purpose IN ('reply', 'memory_proposal')),
    CONSTRAINT invocation_status_ck  CHECK (status IN ('started', 'completed', 'failed')),
    CONSTRAINT invocation_detail_len_ck CHECK (error_detail IS NULL OR length(error_detail) <= 500),
    CONSTRAINT invocation_no_self_retry_ck CHECK (retry_of_invocation_id IS DISTINCT FROM id)
);
CREATE INDEX invocation_turn_ix ON model_invocation (turn_id, seq);
CREATE INDEX invocation_status_ix ON model_invocation (status) WHERE status = 'started';
-- Containment index for the tombstone hash-redaction query (spec D.7).
CREATE INDEX invocation_manifest_gin ON model_invocation USING gin (context_manifest jsonb_path_ops);

CREATE TABLE memory (
    id                uuid PRIMARY KEY,
    scope             text NOT NULL,
    kind              text NOT NULL,
    -- subject/content are NOT NULL while the claim is live; tombstone nulls them.
    -- The check below carries the "not null while live" intent that a plain
    -- NOT NULL could not, because tombstone must be able to remove content.
    subject           text,
    content           text,
    origin_tier       text NOT NULL,
    origin            text NOT NULL,
    status            text NOT NULL,
    pinned            boolean NOT NULL DEFAULT false,
    created_at        timestamptz NOT NULL,
    updated_at        timestamptz NOT NULL,
    last_confirmed_at timestamptz,
    superseded_by_id  uuid REFERENCES memory(id),
    archived_at       timestamptz,
    tombstoned_at     timestamptz,
    search_vector     tsvector GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(subject, '') || ' ' || coalesce(content, ''))
    ) STORED,
    CONSTRAINT memory_scope_ck  CHECK (scope IN ('self', 'user', 'relationship', 'world')),
    CONSTRAINT memory_kind_ck   CHECK (kind IN ('fact', 'preference', 'person', 'project',
                                                'decision', 'constraint', 'event')),
    CONSTRAINT memory_origin_tier_ck CHECK (origin_tier IN ('user_asserted', 'model_inferred',
                                                            'connector_imported')),
    CONSTRAINT memory_origin_ck CHECK (origin IN ('personal', 'fixture')),
    CONSTRAINT memory_status_ck CHECK (status IN ('active', 'superseded', 'archived', 'tombstoned')),
    CONSTRAINT memory_content_presence_ck CHECK (
        (status = 'tombstoned' AND content IS NULL AND subject IS NULL)
        OR (status <> 'tombstoned' AND content IS NOT NULL AND subject IS NOT NULL)
    )
);
CREATE INDEX memory_search_gin ON memory USING gin (search_vector);
CREATE INDEX memory_active_ix ON memory (origin, status) WHERE status = 'active';

CREATE TABLE memory_observation (
    id           uuid PRIMARY KEY,
    memory_id    uuid NOT NULL REFERENCES memory(id),
    relation     text NOT NULL,
    source_kind  text NOT NULL,
    message_id   uuid REFERENCES message(id),
    external_ref text,
    excerpt      text,
    observed_at  timestamptz NOT NULL,
    created_at   timestamptz NOT NULL,
    CONSTRAINT observation_relation_ck CHECK (relation IN ('asserts', 'confirms', 'contradicts')),
    CONSTRAINT observation_source_ck CHECK (source_kind IN (
        'user_message', 'user_direct_entry', 'model_proposal_approved', 'document', 'connector'))
);
CREATE INDEX observation_memory_ix ON memory_observation (memory_id);

CREATE TABLE memory_proposal (
    id                  uuid PRIMARY KEY,
    conversation_id     uuid NOT NULL REFERENCES conversation(id),
    turn_id             uuid NOT NULL REFERENCES turn(id),
    model_invocation_id uuid NOT NULL REFERENCES model_invocation(id),
    source_message_id   uuid NOT NULL REFERENCES message(id),
    scope               text NOT NULL,
    kind                text NOT NULL,
    subject             text,
    content             text,
    status              text NOT NULL,
    resulting_memory_id uuid REFERENCES memory(id),
    created_at          timestamptz NOT NULL,
    resolved_at         timestamptz,
    CONSTRAINT proposal_status_ck CHECK (status IN ('pending', 'saved', 'saved_edited',
                                                    'ignored', 'expired')),
    CONSTRAINT proposal_scope_ck  CHECK (scope IN ('self', 'user', 'relationship', 'world')),
    -- Proposal text is transient: cleared in the resolving transaction (spec C.9).
    CONSTRAINT proposal_text_transient_ck CHECK (
        (status = 'pending' AND content IS NOT NULL)
        OR (status <> 'pending' AND content IS NULL AND subject IS NULL)
    )
);
CREATE INDEX proposal_pending_ix ON memory_proposal (status) WHERE status = 'pending';

CREATE TABLE audit_event (
    id              bigserial PRIMARY KEY,
    event_type      text NOT NULL,
    occurred_at     timestamptz NOT NULL,
    actor           text NOT NULL,
    subject_kind    text NOT NULL,
    subject_id      uuid,
    conversation_id uuid,
    turn_id         uuid,
    payload         jsonb NOT NULL,
    CONSTRAINT audit_actor_ck CHECK (actor IN ('user', 'apollo_core', 'model', 'system'))
);
CREATE INDEX audit_turn_ix ON audit_event (turn_id) WHERE turn_id IS NOT NULL;
CREATE INDEX audit_type_ix ON audit_event (event_type, id);

CREATE TABLE identity_version (
    content_hash    text PRIMARY KEY,
    version_label   text    NOT NULL,
    schema_version  integer NOT NULL,
    content         text    NOT NULL,
    fragments       jsonb   NOT NULL,
    first_loaded_at timestamptz NOT NULL
);

CREATE TABLE turn_retrieval (
    id           uuid PRIMARY KEY,
    turn_id      uuid NOT NULL REFERENCES turn(id),
    strategy     text NOT NULL,
    query_text   text,
    result_count integer NOT NULL,
    substantive  boolean NOT NULL,
    duration_ms  integer NOT NULL,
    executed_at  timestamptz NOT NULL,
    CONSTRAINT retrieval_strategy_ck CHECK (strategy IN ('pinned', 'lexical', 'recency')),
    -- `recency` is never substantive support (spec E.3). This is the
    -- anti-confabulation rule, held by the database as well as by code.
    CONSTRAINT retrieval_recency_not_substantive_ck CHECK (
        strategy <> 'recency' OR substantive = false
    )
);
CREATE INDEX retrieval_turn_ix ON turn_retrieval (turn_id);

CREATE TABLE turn_retrieval_result (
    turn_retrieval_id   uuid NOT NULL REFERENCES turn_retrieval(id),
    memory_id           uuid NOT NULL REFERENCES memory(id),
    rank                integer NOT NULL,
    score               numeric NOT NULL,
    included_in_context boolean NOT NULL,
    PRIMARY KEY (turn_retrieval_id, memory_id)
);

-- ---------------------------------------------------------------------------
-- Write-once enforcement. Reconstruction depends on these columns not moving.
-- ---------------------------------------------------------------------------

CREATE FUNCTION apollo_reject_update() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'apollo: % rows are write-once', TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END;
$$;

-- Messages are write-once in every column (spec C.2).
CREATE TRIGGER message_write_once BEFORE UPDATE ON message
    FOR EACH ROW EXECUTE FUNCTION apollo_reject_update();

-- The identity snapshot is inserted on first sight and never updated (spec C.11).
CREATE TRIGGER identity_version_write_once BEFORE UPDATE ON identity_version
    FOR EACH ROW EXECUTE FUNCTION apollo_reject_update();

CREATE FUNCTION apollo_reject_column_change() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    col text;
BEGIN
    FOREACH col IN ARRAY TG_ARGV LOOP
        IF to_jsonb(OLD) -> col IS DISTINCT FROM to_jsonb(NEW) -> col THEN
            RAISE EXCEPTION 'apollo: %.% is write-once', TG_TABLE_NAME, col
                USING ERRCODE = 'restrict_violation';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$;

-- Conversation mode is immutable: changing it would retroactively alter what
-- data was permitted in turns already taken (spec C.1).
CREATE TRIGGER conversation_mode_immutable BEFORE UPDATE ON conversation
    FOR EACH ROW EXECUTE FUNCTION apollo_reject_column_change('mode');

-- Claim content is write-once; lifecycle metadata is not (spec C.7). Tombstone
-- is the single exception and is allowed through by the WHEN clause.
CREATE TRIGGER memory_claim_write_once BEFORE UPDATE ON memory
    FOR EACH ROW WHEN (NEW.status <> 'tombstoned')
    EXECUTE FUNCTION apollo_reject_column_change(
        'scope', 'kind', 'subject', 'content', 'origin_tier', 'origin', 'created_at');

-- A turn's identity is the identity in force for it and does not move.
CREATE TRIGGER turn_identity_write_once BEFORE UPDATE ON turn
    FOR EACH ROW EXECUTE FUNCTION apollo_reject_column_change(
        'conversation_id', 'request_message_id', 'identity_version', 'identity_hash',
        'conversation_mode', 'started_at');

-- An invocation's bundle identity does not move either; only its outcome does.
-- context_bundle_hash and rendered_prompt_hash are omitted deliberately: the
-- tombstone redaction path (spec D.7) must be able to null them.
CREATE TRIGGER invocation_inputs_write_once BEFORE UPDATE ON model_invocation
    FOR EACH ROW EXECUTE FUNCTION apollo_reject_column_change(
        'turn_id', 'seq', 'purpose', 'retry_of_invocation_id', 'brain_alias', 'provider_key',
        'adapter_key', 'render_version', 'compiler_version', 'token_estimator',
        'context_manifest', 'context_token_estimate', 'max_trust_tier', 'taint',
        'generation_params', 'started_at');
