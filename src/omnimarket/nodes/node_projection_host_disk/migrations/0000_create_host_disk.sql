-- =============================================================================
-- MIGRATION: Host disk projection read models
-- =============================================================================
-- Ticket:  OMN-23865
-- Owner:   omnimarket.nodes.node_projection_host_disk
-- Version: 1.0.0
--
-- WHAT THIS HOLDS
--   node_lab_disk_watermark_effect publishes a host disk reading event per
--   reading. These two tables are the read models the next ticket's compute
--   reads instead of a host:
--
--     host_disk_class   one row per (host_name, class_name): the newest
--                       reading for that host and class, with its status,
--                       unread reason, byte totals, entry count, revision
--                       count, groups, and when it was read
--     host_disk_volume  one row per (host_name, volume_path): the newest
--                       reading for that host and volume, with its byte
--                       totals and when it was read
--
-- WHY read_at IS THE ORDERING AUTHORITY
--   read_at is the timestamp of the reading, so it is a total order over one
--   host and class's readings. A redelivery or an older reading is refused in
--   the upsert WHERE, not in a read-then-write, which two consumers could
--   race.
--
-- Idempotency: CREATE TABLE IF NOT EXISTS plus one guarded ADD COLUMN per
-- declared column. Nothing here touches RLS, ownership or any role attribute.
-- =============================================================================

CREATE TABLE IF NOT EXISTS omninode_internal.host_disk_class (
    host_name              TEXT        NOT NULL,
    class_name             TEXT        NOT NULL,
    status                 TEXT        NOT NULL CHECK (status IN ('read', 'unread')),
    unread_reason          TEXT        NOT NULL DEFAULT '',
    total_bytes            BIGINT      NOT NULL DEFAULT 0,
    entry_count            INTEGER     NOT NULL DEFAULT 0,
    revision_count         INTEGER     NOT NULL DEFAULT 0,
    groups                 JSONB       NOT NULL DEFAULT '[]'::jsonb,
    groups_omitted         INTEGER     NOT NULL DEFAULT 0,
    unreadable_process_count INTEGER   NOT NULL DEFAULT 0,
    read_at                TIMESTAMPTZ NOT NULL,
    correlation_id         TEXT        NOT NULL,
    reading_count          BIGINT      NOT NULL DEFAULT 1,
    first_seen_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor      BIGSERIAL   NOT NULL,

    CONSTRAINT pk_host_disk_class
        PRIMARY KEY (host_name, class_name)
);

ALTER TABLE omninode_internal.host_disk_class
    ADD COLUMN IF NOT EXISTS host_name              TEXT;
ALTER TABLE omninode_internal.host_disk_class
    ADD COLUMN IF NOT EXISTS class_name             TEXT;
ALTER TABLE omninode_internal.host_disk_class
    ADD COLUMN IF NOT EXISTS status                 TEXT;
ALTER TABLE omninode_internal.host_disk_class
    ADD COLUMN IF NOT EXISTS unread_reason          TEXT DEFAULT '';
ALTER TABLE omninode_internal.host_disk_class
    ADD COLUMN IF NOT EXISTS total_bytes            BIGINT DEFAULT 0;
ALTER TABLE omninode_internal.host_disk_class
    ADD COLUMN IF NOT EXISTS entry_count            INTEGER DEFAULT 0;
ALTER TABLE omninode_internal.host_disk_class
    ADD COLUMN IF NOT EXISTS revision_count         INTEGER DEFAULT 0;
ALTER TABLE omninode_internal.host_disk_class
    ADD COLUMN IF NOT EXISTS groups                 JSONB DEFAULT '[]'::jsonb;
ALTER TABLE omninode_internal.host_disk_class
    ADD COLUMN IF NOT EXISTS groups_omitted         INTEGER DEFAULT 0;
ALTER TABLE omninode_internal.host_disk_class
    ADD COLUMN IF NOT EXISTS unreadable_process_count INTEGER DEFAULT 0;
ALTER TABLE omninode_internal.host_disk_class
    ADD COLUMN IF NOT EXISTS read_at                TIMESTAMPTZ;
ALTER TABLE omninode_internal.host_disk_class
    ADD COLUMN IF NOT EXISTS correlation_id         TEXT;
ALTER TABLE omninode_internal.host_disk_class
    ADD COLUMN IF NOT EXISTS reading_count          BIGINT DEFAULT 1;
ALTER TABLE omninode_internal.host_disk_class
    ADD COLUMN IF NOT EXISTS first_seen_at          TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE omninode_internal.host_disk_class
    ADD COLUMN IF NOT EXISTS updated_at             TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE omninode_internal.host_disk_class
    ADD COLUMN IF NOT EXISTS projection_cursor      BIGSERIAL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_host_disk_class_projection_cursor
    ON omninode_internal.host_disk_class (projection_cursor);

CREATE TABLE IF NOT EXISTS omninode_internal.host_disk_volume (
    host_name              TEXT        NOT NULL,
    volume_path            TEXT        NOT NULL,
    total_bytes            BIGINT      NOT NULL,
    free_bytes             BIGINT      NOT NULL,
    used_bytes             BIGINT      NOT NULL,
    read_at                TIMESTAMPTZ NOT NULL,
    correlation_id         TEXT        NOT NULL,
    reading_count          BIGINT      NOT NULL DEFAULT 1,
    first_seen_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    projection_cursor      BIGSERIAL   NOT NULL,

    CONSTRAINT pk_host_disk_volume
        PRIMARY KEY (host_name, volume_path)
);

ALTER TABLE omninode_internal.host_disk_volume
    ADD COLUMN IF NOT EXISTS host_name              TEXT;
ALTER TABLE omninode_internal.host_disk_volume
    ADD COLUMN IF NOT EXISTS volume_path            TEXT;
ALTER TABLE omninode_internal.host_disk_volume
    ADD COLUMN IF NOT EXISTS total_bytes            BIGINT;
ALTER TABLE omninode_internal.host_disk_volume
    ADD COLUMN IF NOT EXISTS free_bytes             BIGINT;
ALTER TABLE omninode_internal.host_disk_volume
    ADD COLUMN IF NOT EXISTS used_bytes             BIGINT;
ALTER TABLE omninode_internal.host_disk_volume
    ADD COLUMN IF NOT EXISTS read_at                TIMESTAMPTZ;
ALTER TABLE omninode_internal.host_disk_volume
    ADD COLUMN IF NOT EXISTS correlation_id         TEXT;
ALTER TABLE omninode_internal.host_disk_volume
    ADD COLUMN IF NOT EXISTS reading_count          BIGINT DEFAULT 1;
ALTER TABLE omninode_internal.host_disk_volume
    ADD COLUMN IF NOT EXISTS first_seen_at          TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE omninode_internal.host_disk_volume
    ADD COLUMN IF NOT EXISTS updated_at             TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE omninode_internal.host_disk_volume
    ADD COLUMN IF NOT EXISTS projection_cursor      BIGSERIAL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_host_disk_volume_projection_cursor
    ON omninode_internal.host_disk_volume (projection_cursor);

COMMENT ON TABLE omninode_internal.host_disk_class IS
    'OMN-23865: one row per (host_name, class_name) for the host disk projection. '
    'read_at is the only ordering authority; a stale or redelivered reading is '
    'refused in the upsert WHERE. Written only by node_projection_host_disk.';

COMMENT ON TABLE omninode_internal.host_disk_volume IS
    'OMN-23865: one row per (host_name, volume_path) for the host disk projection. '
    'read_at is the only ordering authority; a stale or redelivered reading is '
    'refused in the upsert WHERE. Written only by node_projection_host_disk.';
