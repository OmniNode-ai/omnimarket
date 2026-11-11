-- OMN-23055: omninode_runtime grants for the four gate store relations.
-- Target DB: omnidash_analytics (NODE_POSTGRES_DB)
-- Node: node_gate_store_effect
--
-- Declaring a grant in the topology is not issuing one (OMN-17377, OMN-17379), so the
-- grants ride in the owning node's lineage, beside the file that creates the relations.
--
-- gate_store_state and gate_store_reservations are read and updated by every request;
-- gate_store_safety is upserted; gate_store_steps is append-only (a recorded first answer
-- is never changed), so it gets SELECT and INSERT only. Nothing gets DELETE except the
-- reservations, which a release or an expiry removes.
--
-- Idempotency: GRANT is idempotent. Nothing here touches RLS, ownership, or any role attribute.

GRANT USAGE ON SCHEMA omninode_internal TO omninode_runtime;

GRANT SELECT, UPDATE
    ON omninode_internal.gate_store_state
    TO omninode_runtime;

GRANT SELECT, INSERT, UPDATE, DELETE
    ON omninode_internal.gate_store_reservations
    TO omninode_runtime;

GRANT SELECT, INSERT, UPDATE
    ON omninode_internal.gate_store_safety
    TO omninode_runtime;

GRANT SELECT, INSERT
    ON omninode_internal.gate_store_steps
    TO omninode_runtime;
