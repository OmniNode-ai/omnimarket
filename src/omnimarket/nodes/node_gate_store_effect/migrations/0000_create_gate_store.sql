-- =============================================================================
-- MIGRATION: Gate store (rebuild plan section 7.5, step 0 row 3)
-- =============================================================================
-- Ticket:  OMN-23055
-- Owner:   omnimarket.nodes.node_gate_store_effect
-- Version: 1.0.0
--
-- WHAT THIS HOLDS
--   One serializable store behind one effect: the epoch, the holder record and
--   the order position (gate_store_state, one row), the reservations and their
--   consumed flag (gate_store_reservations), the stop and safety state per piece
--   (gate_store_safety) and the step keys with the first answer each recorded
--   (gate_store_steps).
--
-- WHY ONE ROW LOCK
--   Every request locks the single gate_store_state row first (SELECT ... FOR
--   UPDATE) and applies its whole change before the commit, so two requests are
--   never decided on the same snapshot: nine racing reservations stay within the
--   limit, and a request that is killed before the commit leaves no trace.
--
-- Idempotency: CREATE TABLE IF NOT EXISTS, then ALTER TABLE ... ADD COLUMN IF NOT
-- EXISTS per declared column so a drifted pre-existing table is reconciled. Nothing here touches RLS, ownership
-- or any role attribute. No limit, lifetime or order is stored: those are the
-- gate_store overlay's and arrive with each request.
-- =============================================================================

CREATE TABLE IF NOT EXISTS omninode_internal.gate_store_state (
    singleton       BOOLEAN          PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    epoch           BIGINT           NOT NULL DEFAULT 0,
    holder          TEXT,
    order_position  INTEGER          NOT NULL DEFAULT 0
);


-- Reconcile a drifted pre-existing table: CREATE TABLE IF NOT EXISTS no-ops there.
ALTER TABLE omninode_internal.gate_store_state ADD COLUMN IF NOT EXISTS singleton BOOLEAN DEFAULT TRUE;
ALTER TABLE omninode_internal.gate_store_state ADD COLUMN IF NOT EXISTS epoch BIGINT DEFAULT 0;
ALTER TABLE omninode_internal.gate_store_state ADD COLUMN IF NOT EXISTS holder TEXT;
ALTER TABLE omninode_internal.gate_store_state ADD COLUMN IF NOT EXISTS order_position INTEGER DEFAULT 0;
INSERT INTO omninode_internal.gate_store_state (singleton)
VALUES (TRUE)
ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS omninode_internal.gate_store_reservations (
    reservation_id  TEXT             PRIMARY KEY,
    piece           TEXT             NOT NULL,
    expires_at      DOUBLE PRECISION NOT NULL,
    consumed        BOOLEAN          NOT NULL DEFAULT FALSE
);


ALTER TABLE omninode_internal.gate_store_reservations ADD COLUMN IF NOT EXISTS reservation_id TEXT;
ALTER TABLE omninode_internal.gate_store_reservations ADD COLUMN IF NOT EXISTS piece TEXT;
ALTER TABLE omninode_internal.gate_store_reservations ADD COLUMN IF NOT EXISTS expires_at DOUBLE PRECISION;
ALTER TABLE omninode_internal.gate_store_reservations ADD COLUMN IF NOT EXISTS consumed BOOLEAN DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS gate_store_reservations_piece_idx
    ON omninode_internal.gate_store_reservations (piece);

CREATE TABLE IF NOT EXISTS omninode_internal.gate_store_safety (
    piece           TEXT             PRIMARY KEY,
    safety_state    TEXT             NOT NULL CHECK (safety_state IN ('stop', 'clear'))
);


ALTER TABLE omninode_internal.gate_store_safety ADD COLUMN IF NOT EXISTS piece TEXT;
ALTER TABLE omninode_internal.gate_store_safety ADD COLUMN IF NOT EXISTS safety_state TEXT;
CREATE TABLE IF NOT EXISTS omninode_internal.gate_store_steps (
    step_key        TEXT             PRIMARY KEY,
    result          JSONB            NOT NULL
);


ALTER TABLE omninode_internal.gate_store_steps ADD COLUMN IF NOT EXISTS step_key TEXT;
ALTER TABLE omninode_internal.gate_store_steps ADD COLUMN IF NOT EXISTS result JSONB;
