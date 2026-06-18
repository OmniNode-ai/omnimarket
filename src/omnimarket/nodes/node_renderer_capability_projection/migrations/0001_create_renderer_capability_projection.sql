-- OMN-13131 / W5: Renderer Capability Registry projection.
-- Sole writer: node_renderer_capability_projection (NodeReducer). This table IS
-- the registry — there is no in-memory CapabilityRegistry class. Consumers read
-- the materialized projection via GET /projection/onex.evt.omnimarket.renderer-capability-projection-snapshot.v1.
-- One row per renderer_id (UPSERT key); heartbeat-TTL freshness is materialized
-- as is_degraded + a typed empty_state_reason ('upstream-blocked' when degraded).

CREATE TABLE IF NOT EXISTS renderer_capability_projection (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    -- Upsert key — one row per renderer.
    renderer_id                 TEXT NOT NULL,

    -- Declared capability surface (mirrors ModelRendererCapabilityContract).
    platform                    TEXT NOT NULL,
    supported_component_kinds   TEXT[] NOT NULL DEFAULT '{}',
    interaction_model           TEXT NOT NULL,
    accessibility_tier          TEXT NOT NULL,
    contract_version            TEXT NOT NULL,

    -- Heartbeat freshness.
    declared_at                 TIMESTAMPTZ NOT NULL,
    last_heartbeat              TIMESTAMPTZ NOT NULL,
    is_degraded                 BOOLEAN NOT NULL DEFAULT FALSE,
    empty_state_reason          TEXT,                    -- typed EnumEmptyStateReason value; 'upstream-blocked' when degraded

    -- Projection lineage.
    observed_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_renderer_capability_renderer_id UNIQUE (renderer_id)
);

CREATE INDEX IF NOT EXISTS ix_renderer_capability_last_heartbeat
    ON renderer_capability_projection (last_heartbeat DESC);
