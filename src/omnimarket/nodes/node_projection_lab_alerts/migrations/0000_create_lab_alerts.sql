-- One row per lab alert, per source and subject, folded from
-- onex.evt.omnimarket.lab-alert-transitioned.v1.
-- Owner: omnimarket.nodes.node_projection_lab_alerts
-- Target database: omnidash_analytics; physical schema: omninode_internal.
--
-- The key is alert_id, derived from (source, subject), so a repeated condition
-- updates one row. A row is a function of the newest transition for its key,
-- ordered by (last_transition_at, last_transition_id): a replayed or older
-- transition writes nothing. An alert clears only from a later transition;
-- nothing else changes state. A raise after a clear opens a new episode on the
-- same row. Like lab_lane_health the relation is net-new and created directly in
-- omninode_internal.

CREATE TABLE IF NOT EXISTS omninode_internal.lab_alerts (
    alert_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source <> ''),
    subject TEXT NOT NULL CHECK (subject <> ''),
    state TEXT NOT NULL CHECK (state IN ('open', 'cleared')),
    severity TEXT NOT NULL CHECK (severity IN ('SEV1', 'SEV2', 'SEV3', 'SEV4')),
    owner TEXT NOT NULL CHECK (btrim(owner) <> ''),
    detail TEXT NOT NULL CHECK (btrim(detail) <> ''),
    raised_at TIMESTAMPTZ NOT NULL,
    cleared_at TIMESTAMPTZ,
    last_transition_id UUID NOT NULL,
    last_transition_at TIMESTAMPTZ NOT NULL,
    projected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (alert_id),
    -- A cleared alert carries its clear time and an open one never does, and an
    -- alert never clears before it was raised.
    CONSTRAINT lab_alerts_cleared_complete_check CHECK (
        (state = 'cleared') = (cleared_at IS NOT NULL)
    ),
    CONSTRAINT lab_alerts_cleared_after_raised_check CHECK (
        cleared_at IS NULL OR cleared_at >= raised_at
    )
);

CREATE INDEX IF NOT EXISTS idx_lab_alerts_state_raised
    ON omninode_internal.lab_alerts (state, raised_at DESC);
