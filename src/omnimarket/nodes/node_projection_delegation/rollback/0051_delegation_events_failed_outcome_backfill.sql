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
-- Row-level security is read the way 0051 reads it and never changed: a role
-- that bypasses it restores in one pass, any other role restores tenant by
-- tenant through tenant_registry_mirror.
--
-- Manual execution only. rollback/ is not mounted to docker-entrypoint-initdb.d
-- and no runner reads it. It does not remove 0051's ledger row, so the node
-- loop will not re-apply 0051 unless that row is removed too. Run it as the
-- role that ran 0051.

DO $$
DECLARE
    v_blind BOOLEAN;
    v_tenants UUID[];
    v_tenant UUID;
    v_restored BIGINT := 0;
    v_step BIGINT;
BEGIN
    IF to_regclass('omninode_internal.delegation_events_outcome_backfill_omn20276') IS NULL THEN
        RAISE NOTICE 'OMN-20276: no backfill audit table; nothing to restore';
        RETURN;
    END IF;

    IF to_regclass('delegation_events') IS NOT NULL THEN
        SELECT c.relrowsecurity
           AND NOT (r.rolsuper OR r.rolbypassrls)
           AND (c.relforcerowsecurity OR NOT pg_catalog.pg_has_role(current_user, c.relowner, 'MEMBER'))
          INTO v_blind
          FROM pg_catalog.pg_class AS c, pg_catalog.pg_roles AS r
         WHERE c.oid = 'delegation_events'::regclass
           AND r.rolname = current_user;

        IF v_blind AND to_regclass('tenant_registry_mirror') IS NOT NULL THEN
            v_tenants := ARRAY(
                SELECT DISTINCT m.tenant_uuid FROM tenant_registry_mirror AS m
                WHERE m.tenant_uuid IS NOT NULL
            );
        ELSIF v_blind THEN
            RAISE EXCEPTION 'OMN-20276: row-level security hides delegation_events from % and '
                'tenant_registry_mirror is absent; run this as the role that ran 0051', current_user;
        ELSE
            v_tenants := ARRAY[NULL::UUID];
        END IF;

        FOREACH v_tenant IN ARRAY v_tenants LOOP
            IF v_tenant IS NOT NULL THEN
                PERFORM pg_catalog.set_config('app.tenant_id', v_tenant::text, true);
            END IF;
            UPDATE delegation_events AS e
            SET operational_outcome = a.prior_operational_outcome,
                content_verdict = a.prior_content_verdict
            FROM omninode_internal.delegation_events_outcome_backfill_omn20276 AS a
            WHERE e.id = a.delegation_event_id
              AND e.operational_outcome IS NOT DISTINCT FROM a.new_operational_outcome
              AND e.content_verdict IS NOT DISTINCT FROM a.new_content_verdict;
            GET DIAGNOSTICS v_step = ROW_COUNT;
            v_restored := v_restored + v_step;
        END LOOP;
        RAISE NOTICE 'OMN-20276: restored % delegation_events rows', v_restored;
    END IF;

    DROP TABLE omninode_internal.delegation_events_outcome_backfill_omn20276;
END$$;
