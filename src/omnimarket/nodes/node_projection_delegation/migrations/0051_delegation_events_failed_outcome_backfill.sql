-- OMN-20276: rewrite the delegation_events rows that contradict themselves.
--
-- WHAT IS WRONG WITH THESE ROWS
--
--   One delegation_events row (upsert key correlation_id) is written by two
--   event families. The inner delegation terminal names operational_outcome
--   and content_verdict; the outer delegate-skill terminal names terminal_ok
--   and terminal_failure_cause. Before omnimarket#3158 (OMN-19559) nothing
--   reconciled them, so a row could read terminal_ok=false with a typed
--   failure cause while operational_outcome='completed' and
--   content_verdict='usable'. Read-only psql on the .201 dev lane at
--   2026-10-01T10:36Z found 249 such rows (timeout 194, runtime_shutdown 46,
--   provider_error 9), every one written before #3158 merged.
--
-- WHAT THIS FILE DOES
--
--   It applies to those historical rows exactly the rule #3158 applies to new
--   ones (model_terminal_precedence.apply_terminal_precedence and
--   outcome_for_failure_cause): a row whose terminal_ok is false, whose cause
--   is not blank and whose outcome is 'completed' takes the outcome its cause
--   names, and a 'usable' or 'correct' verdict takes the cause's verdict.
--   tests/test_omn19559_backfill_migration_real_postgres.py fails if the
--   mapping below and outcome_for_failure_cause disagree for any cause.
--
--   Each rewritten row's prior outcome and verdict are kept first in
--   omninode_internal.delegation_events_outcome_backfill_omn20276, and the
--   rewrite reads its new values from there, so the rollback (omnibase_infra
--   rollback_node_projection_delegation_0051.sql) restores them exactly and
--   drops that table. The table is internal control state for this migration
--   pair, with no tenant_id and no row-level security.
--
-- RE-RUNNABLE
--
--   A second application matches nothing: the first left no row with
--   terminal_ok=false and outcome 'completed'. The audit insert keeps the
--   first prior values (ON CONFLICT DO NOTHING), so a re-run never overwrites
--   what the rollback needs.
--
-- THE SCHEMA IS ASSERTED, NOT CREATED
--
--   omninode_internal is provisioned by omnibase_infra forward 098; a node
--   migration does not create a shared namespace (see
--   node_delegate_skill_orchestrator 0001). Without it the CREATE TABLE below
--   fails, so no lane can record this file applied while its rows stay wrong.
--
-- ROW LEVEL SECURITY, WITHOUT TOUCHING IT
--
--   delegation_events carries a tenant_isolation policy on the tenant lanes,
--   and under FORCE the owner is filtered too: with app.tenant_id unset every
--   row is invisible and a plain rewrite would match nothing and report
--   success. This file never changes the relation's row-level security (a
--   file that turns FORCE on is fenced, and lifting it is a window). A role
--   that bypasses RLS (a superuser, as the compose runner is) or owns a
--   non-FORCE relation sees every row and rewrites once. Any other role
--   rewrites tenant by tenant, setting app.tenant_id transaction-locally for
--   each tenant tenant_registry_mirror resolves, the identity source 0033
--   uses. A row whose tenant the mirror does not hold stays as it is, and the
--   NOTICE says the rewrite ran blind-safe. created_at and the row's other
--   columns are not touched.

CREATE TABLE IF NOT EXISTS omninode_internal.delegation_events_outcome_backfill_omn20276 (
    delegation_event_id UUID PRIMARY KEY,
    correlation_id TEXT NOT NULL,
    prior_operational_outcome TEXT,
    prior_content_verdict TEXT,
    new_operational_outcome TEXT NOT NULL,
    new_content_verdict TEXT,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---- BEGIN OMN-15376 shape reconciliation: delegation_events_outcome_backfill_omn20276 ----
ALTER TABLE omninode_internal.delegation_events_outcome_backfill_omn20276 ADD COLUMN IF NOT EXISTS delegation_event_id UUID;
ALTER TABLE omninode_internal.delegation_events_outcome_backfill_omn20276 ADD COLUMN IF NOT EXISTS correlation_id TEXT;
ALTER TABLE omninode_internal.delegation_events_outcome_backfill_omn20276 ADD COLUMN IF NOT EXISTS prior_operational_outcome TEXT;
ALTER TABLE omninode_internal.delegation_events_outcome_backfill_omn20276 ADD COLUMN IF NOT EXISTS prior_content_verdict TEXT;
ALTER TABLE omninode_internal.delegation_events_outcome_backfill_omn20276 ADD COLUMN IF NOT EXISTS new_operational_outcome TEXT;
ALTER TABLE omninode_internal.delegation_events_outcome_backfill_omn20276 ADD COLUMN IF NOT EXISTS new_content_verdict TEXT;
ALTER TABLE omninode_internal.delegation_events_outcome_backfill_omn20276 ADD COLUMN IF NOT EXISTS applied_at TIMESTAMPTZ DEFAULT now();
-- ---- END OMN-15376 shape reconciliation: delegation_events_outcome_backfill_omn20276 ----

-- Migration 099's default privileges hand omninode_runtime read/write on every new
-- omninode_internal table. This one is control state for this migration pair, read
-- only by its rollback, so the runtime role gets nothing on it.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_catalog.pg_roles WHERE rolname = 'omninode_runtime') THEN
        REVOKE ALL ON omninode_internal.delegation_events_outcome_backfill_omn20276 FROM omninode_runtime;
    END IF;
END$$;

COMMENT ON TABLE omninode_internal.delegation_events_outcome_backfill_omn20276 IS
    'OMN-20276: prior outcome and verdict of each delegation_events row 0051 rewrote; read by its rollback, dropped by it.';

DO $$
DECLARE
    v_blind BOOLEAN;
    v_tenants UUID[];
    v_tenant UUID;
    v_rewritten BIGINT := 0;
    v_step BIGINT;
BEGIN
    IF to_regclass('delegation_events') IS NULL THEN
        RAISE NOTICE 'OMN-20276: delegation_events does not exist; nothing to rewrite';
        RETURN;
    END IF;
    -- A replay that withholds 0029 or 0045 has no column to contradict.
    IF (
        SELECT count(*) FROM pg_catalog.pg_attribute
        WHERE attrelid = 'delegation_events'::regclass
          AND attname IN (
              'terminal_ok', 'terminal_failure_cause',
              'operational_outcome', 'content_verdict'
          )
          AND NOT attisdropped
    ) < 4 THEN
        RAISE NOTICE 'OMN-20276: delegation_events has no outcome columns; nothing to rewrite';
        RETURN;
    END IF;

    SELECT c.relrowsecurity
       AND NOT (r.rolsuper OR r.rolbypassrls)
       AND (c.relforcerowsecurity OR NOT pg_catalog.pg_has_role(current_user, c.relowner, 'MEMBER'))
      INTO v_blind
      FROM pg_catalog.pg_class AS c, pg_catalog.pg_roles AS r
     WHERE c.oid = 'delegation_events'::regclass
       AND r.rolname = current_user;

    IF v_blind THEN
        IF to_regclass('tenant_registry_mirror') IS NULL THEN
            RAISE NOTICE 'OMN-20276: row-level security hides delegation_events from %, and '
                'tenant_registry_mirror is absent; nothing rewritten', current_user;
            RETURN;
        END IF;
        v_tenants := ARRAY(
            SELECT DISTINCT m.tenant_uuid FROM tenant_registry_mirror AS m
            WHERE m.tenant_uuid IS NOT NULL
        );
    ELSE
        v_tenants := ARRAY[NULL::UUID];
    END IF;

    FOREACH v_tenant IN ARRAY v_tenants LOOP
        IF v_tenant IS NOT NULL THEN
            PERFORM pg_catalog.set_config('app.tenant_id', v_tenant::text, true);
        END IF;

        INSERT INTO omninode_internal.delegation_events_outcome_backfill_omn20276 (
            delegation_event_id, correlation_id,
            prior_operational_outcome, prior_content_verdict,
            new_operational_outcome, new_content_verdict
        )
        SELECT
            e.id,
            e.correlation_id,
            e.operational_outcome,
            e.content_verdict,
            CASE e.terminal_failure_cause
                WHEN 'timeout' THEN 'timeout'
                WHEN 'runtime_shutdown' THEN 'cancelled'
                WHEN 'provider_quota_exhausted' THEN 'provider_quota'
                WHEN 'quality_gate_refused' THEN 'quality_rejected'
                ELSE 'inference_failed'
            END,
            CASE
                WHEN e.content_verdict IN ('usable', 'correct') THEN
                    CASE e.terminal_failure_cause
                        WHEN 'quality_gate_refused' THEN 'unusable'
                        ELSE 'not_applicable'
                    END
                ELSE e.content_verdict
            END
        FROM delegation_events AS e
        WHERE e.terminal_ok IS FALSE
          AND btrim(coalesce(e.terminal_failure_cause, '')) <> ''
          AND e.operational_outcome = 'completed'
        ON CONFLICT (delegation_event_id) DO NOTHING;

        UPDATE delegation_events AS e
        SET operational_outcome = a.new_operational_outcome,
            content_verdict = a.new_content_verdict
        FROM omninode_internal.delegation_events_outcome_backfill_omn20276 AS a
        WHERE e.id = a.delegation_event_id
          AND e.terminal_ok IS FALSE
          AND btrim(coalesce(e.terminal_failure_cause, '')) <> ''
          AND e.operational_outcome = 'completed';
        GET DIAGNOSTICS v_step = ROW_COUNT;
        v_rewritten := v_rewritten + v_step;
    END LOOP;

    IF v_blind THEN
        RAISE NOTICE 'OMN-20276: rewrote % delegation_events rows tenant by tenant (% tenants in '
            'tenant_registry_mirror); a row of a tenant the mirror does not hold is unchanged',
            v_rewritten, cardinality(v_tenants);
    ELSE
        RAISE NOTICE 'OMN-20276: rewrote % delegation_events rows', v_rewritten;
    END IF;
END$$;
