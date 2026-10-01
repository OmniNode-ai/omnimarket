-- OMN-20276: rollback for
-- nodes/node_projection_delegation/0051_delegation_events_failed_outcome_backfill.sql.
--
-- Restores each delegation_events row 0051 rewrote to the operational_outcome
-- and content_verdict it held before, from the audit table 0051 wrote, then
-- drops that table, so the catalog is what it was before 0051. The schema
-- omninode_internal itself is not dropped: 0051 asserted it, never created it.
--
-- A row the projection has written again since 0051 (its outcome or verdict
-- no longer equals what 0051 set) is left alone: the newer terminal is the
-- truth for it, and restoring the old value would overwrite real evidence.
--
-- Manual execution only. rollback/ is not mounted to docker-entrypoint-initdb.d
-- and no runner reads it. It does not remove 0051's ledger row, so the node
-- loop will not re-apply 0051 unless that row is removed too. Run as the
-- table's owner, a member of the owner role, or a superuser. FORCE ROW LEVEL
-- SECURITY is lifted and restored inside one statement, as in 0051.

DO $$
DECLARE
    v_forced BOOLEAN;
    v_restored BIGINT;
BEGIN
    IF to_regclass('omninode_internal.delegation_events_outcome_backfill_omn20276') IS NULL THEN
        RAISE NOTICE 'OMN-20276: no backfill audit table; nothing to restore';
        RETURN;
    END IF;

    IF to_regclass('delegation_events') IS NOT NULL THEN
        v_forced := (
            SELECT relforcerowsecurity FROM pg_catalog.pg_class
            WHERE oid = 'delegation_events'::regclass
        );
        IF v_forced THEN
            ALTER TABLE delegation_events NO FORCE ROW LEVEL SECURITY;
        END IF;

        UPDATE delegation_events AS e
        SET operational_outcome = a.prior_operational_outcome,
            content_verdict = a.prior_content_verdict
        FROM omninode_internal.delegation_events_outcome_backfill_omn20276 AS a
        WHERE e.id = a.delegation_event_id
          AND e.operational_outcome IS NOT DISTINCT FROM a.new_operational_outcome
          AND e.content_verdict IS NOT DISTINCT FROM a.new_content_verdict;
        GET DIAGNOSTICS v_restored = ROW_COUNT;

        IF v_forced THEN
            ALTER TABLE delegation_events FORCE ROW LEVEL SECURITY;
        END IF;
        RAISE NOTICE 'OMN-20276: restored % delegation_events rows', v_restored;
    END IF;

    DROP TABLE omninode_internal.delegation_events_outcome_backfill_omn20276;
END$$;
