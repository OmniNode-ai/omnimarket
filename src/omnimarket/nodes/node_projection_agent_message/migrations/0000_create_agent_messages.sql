-- One row per record of the agent message topic: the message, or the counted trace of a record that could not be read.
-- Every statement can run twice. Every relation is schema-qualified. Nothing is dropped, renamed or rewritten.
-- source_partition and source_offset are the record's place on the topic and the primary key, so a replay of a record is
-- one row and the same offset on another partition is another row. The column names avoid SQL reserved words
-- (from, to, offset, partition).

CREATE TABLE IF NOT EXISTS public.agent_messages (
    source_partition INTEGER NOT NULL,
    source_offset BIGINT NOT NULL,
    status VARCHAR(16) NOT NULL,
    refusal_reason VARCHAR(16),
    refusal_detail TEXT,
    from_agent VARCHAR(200),
    to_agent VARCHAR(200),
    thread VARCHAR(200),
    kind VARCHAR(16),
    platform VARCHAR(16),
    text TEXT,
    sent_at TIMESTAMPTZ,
    broker_at TIMESTAMPTZ,
    sender_verified BOOLEAN NOT NULL DEFAULT FALSE,
    projected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (source_partition, source_offset),
    CONSTRAINT agent_messages_status_known CHECK (status IN ('ok', 'refused')),
    CONSTRAINT agent_messages_refusal_reason_matches_status CHECK (
        (status = 'ok' AND refusal_reason IS NULL)
        OR (status = 'refused' AND refusal_reason IN ('dropped', 'unreadable', 'refused'))
    )
);

CREATE INDEX IF NOT EXISTS idx_agent_messages_sent_at ON public.agent_messages (sent_at);
CREATE INDEX IF NOT EXISTS idx_agent_messages_thread ON public.agent_messages (thread, sent_at);
CREATE INDEX IF NOT EXISTS idx_agent_messages_projected_at ON public.agent_messages (projected_at);
