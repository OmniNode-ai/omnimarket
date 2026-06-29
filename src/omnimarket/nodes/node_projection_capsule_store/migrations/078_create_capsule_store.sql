-- OMN-12842 (M2): durable, scored capsule/exemplar store projection.
--
-- A capsule is identified by a deterministic capsule_hash; a changed exemplar
-- (different content / commit / artifact / schema_version) is a NEW row, never
-- an in-place mutation. Effectiveness is populated from context-ROI score
-- events and is NEVER empty on a scored row -- enforced by a CHECK constraint,
-- not a comment. Raw scored values are immutable in this base table; staleness
-- decay is applied at read time by the projection read view.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS capsule_store (
    capsule_id UUID PRIMARY KEY,
    capsule_hash TEXT NOT NULL,
    factor TEXT NOT NULL,
    source_commit TEXT NOT NULL,
    source_artifact TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    validity_scope TEXT NOT NULL,
    success_rate NUMERIC(6, 5) NOT NULL
        CHECK (success_rate >= 0 AND success_rate <= 1),
    first_pass_rate NUMERIC(6, 5) NOT NULL
        CHECK (first_pass_rate >= 0 AND first_pass_rate <= 1),
    cost_per_success NUMERIC(18, 6) NOT NULL CHECK (cost_per_success >= 0),
    hit_count BIGINT NOT NULL CHECK (hit_count >= 1),
    last_scored TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- "No empty effectiveness fields" encoded in the schema: a scored row
    -- (hit_count >= 1) must carry all effectiveness values. The NOT NULL
    -- columns already guarantee this; the explicit constraint documents and
    -- enforces the scored-row invariant for future nullable migrations.
    CONSTRAINT capsule_store_scored_effectiveness_non_null
        CHECK (
            hit_count >= 1
            AND success_rate IS NOT NULL
            AND first_pass_rate IS NOT NULL
            AND cost_per_success IS NOT NULL
            AND last_scored IS NOT NULL
        )
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_capsule_identity
    ON capsule_store (capsule_hash);

CREATE INDEX IF NOT EXISTS ix_capsule_store_factor_scope
    ON capsule_store (factor, validity_scope);

CREATE OR REPLACE FUNCTION refresh_capsule_store_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_capsule_store_updated_at ON capsule_store;
CREATE TRIGGER trg_capsule_store_updated_at
    BEFORE UPDATE ON capsule_store
    FOR EACH ROW
    EXECUTE FUNCTION refresh_capsule_store_updated_at();
-- The decay read view (projection_capsule_effectiveness) is created in the
-- next migration (079) so the base table exists before any view that reads it
-- (enforced by scripts/ci/check_migration_base_before_view.py, OMN-12942).
