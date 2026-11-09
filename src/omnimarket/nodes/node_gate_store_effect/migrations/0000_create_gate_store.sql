-- SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
-- SPDX-License-Identifier: MIT
-- The gate store's tables (rebuild plan section 7.5). One row of meta holds the epoch, the
-- holder and the order position; every compare is made inside one SERIALIZABLE transaction.
CREATE TABLE IF NOT EXISTS gate_store_meta (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    epoch bigint NOT NULL DEFAULT 0,
    holder text,
    order_position integer NOT NULL DEFAULT 0
);

INSERT INTO gate_store_meta (singleton) VALUES (true) ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS gate_store_reservation (
    reservation_id text PRIMARY KEY,
    piece text NOT NULL,
    expires_at double precision NOT NULL,
    consumed boolean NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS gate_store_reservation_piece_idx
    ON gate_store_reservation (piece);

CREATE TABLE IF NOT EXISTS gate_store_safety (
    piece text PRIMARY KEY,
    state text NOT NULL
);

CREATE TABLE IF NOT EXISTS gate_store_step (
    step_key text PRIMARY KEY,
    result jsonb NOT NULL
);
