-- OMN-16146: create the missing projection_watermarks table.
--
-- WHY THIS EXISTS
--   omnimarket.projection.runner.BaseProjectionRunner._update_watermark()
--   upserts into projection_watermarks after every committed batch, for
--   EVERY projection writer node (registration, llm-cost, savings,
--   delegation, baselines, routing-decision, session-outcome, ...) -- see
--   docker-compose.projection.yml, all seven services share one
--   OMNIDASH_ANALYTICS_DB_URL database. The table was never migrated in
--   this repo (grep across git history finds no prior
--   "CREATE TABLE ... projection_watermarks" here), so on .201 dev Postgres
--   the writer logged "Failed to update watermark: relation
--   projection_watermarks does not exist" on every batch. Projection rows
--   still landed (the INSERT is wrapped in a try/except that only warns),
--   but watermark persistence was silently dead, so a writer restart could
--   not resume from a persisted watermark.
--
--   This is one physical table in the shared DB, not a per-node schema, so
--   one migration -- owned here by node_projection_registration, the writer
--   named in OMN-16146 -- creates it once for every projection writer that
--   depends on it.
--
--   Column set and the ON CONFLICT (projection_name) target are taken
--   verbatim from BaseProjectionRunner._update_watermark's INSERT statement
--   (src/omnimarket/projection/runner.py):
--     INSERT INTO projection_watermarks
--       (projection_name, last_offset, events_projected, updated_at)
--     VALUES ($1, $2, 1, NOW())
--     ON CONFLICT (projection_name) DO UPDATE SET
--       last_offset = GREATEST(projection_watermarks.last_offset, EXCLUDED.last_offset),
--       events_projected = projection_watermarks.events_projected + 1,
--       last_projected_at = NOW(), updated_at = NOW()
--
-- Idempotency: CREATE TABLE / INDEX are guarded so the migration is safe on
-- a DB where the table already exists and on a fresh omnidash_analytics.

CREATE TABLE IF NOT EXISTS projection_watermarks (
    projection_name TEXT PRIMARY KEY,

    last_offset BIGINT NOT NULL DEFAULT 0,
    events_projected BIGINT NOT NULL DEFAULT 0,

    last_projected_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT non_negative_projection_watermarks_last_offset CHECK (last_offset >= 0),
    CONSTRAINT non_negative_projection_watermarks_events_projected CHECK (events_projected >= 0)
);

CREATE INDEX IF NOT EXISTS idx_projection_watermarks_updated_at
    ON projection_watermarks (updated_at DESC);
