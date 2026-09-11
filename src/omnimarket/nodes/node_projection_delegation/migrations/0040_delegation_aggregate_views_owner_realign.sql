-- OMN-18159: the four aggregate views go back to the base table's owner.
--
-- WHAT THIS CLOSES
-- 0039 had to DROP and CREATE each view rather than CREATE OR REPLACE, because
-- Postgres refuses a replace that changes the column list and every one of
-- them gained `tenant_id`. A DROP discards the view's OWNER along with its
-- privileges. 0039 restored the privileges at its foot; it did not restore the
-- owner, and the owner is not a cosmetic property of a view.
--
-- WHY THAT MATTERS, AND WHY THIS IS A SECURITY FIX RATHER THAN TIDYING
-- A view reads its base tables with the privileges of the VIEW'S OWNER. The
-- role that reaches 0039 is not the role that created these views: 0032, 0033
-- and 0034 each issue `RESET ROLE`, so everything applied after them runs as
-- the migration runner's own identity, which on a lane and in this repo's
-- fixtures is a SUPERUSER. `delegation_events` carries FORCE ROW LEVEL
-- SECURITY precisely so that even its owner is filtered -- but a superuser
-- bypasses row-level security unconditionally. So after 0039 a read through
-- any of these four views sees EVERY tenant's rows regardless of
-- `app.tenant_id`, which is the unscoped-serving leak the whole of OMN-18159
-- exists to remove, reintroduced by the mechanism that removed it.
--
-- MEASURED, not argued. Applying this node's migration directory to a clean
-- postgres:16-alpine under `SET ROLE <migrator>`:
--   without 0039: all four views owned by <migrator>, relacl NULL
--   with 0039:    all four owned by `postgres`, while `delegation_events`
--                 stays owned by <migrator>
-- The divergence is the defect. It also denied the migrator's own reads --
-- three tests in tests/test_omn18139_real_postgres_tenant_guc_representation.py
-- failed with `permission denied for view projection_delegation_summary` --
-- which is how it was found.
--
-- WHY A NEW FILE AND NOT AN EDIT TO 0039
-- 0039 is applied on the .201 dev lane with a recorded `content_sha256` and is
-- declared in the migration manifest, so `check_migration_append_only.py`
-- refuses any modification to its bytes and the forward runner would raise
-- `conflicting migration checksum in canonical node history`. The repair is
-- therefore expressed additively, which is also what makes it correct for a
-- lane that already applied 0039.
--
-- IDEMPOTENT. Realigns only where the owners actually differ, so a re-run and
-- a lane that never diverged are both no-ops. FAIL-CLOSED: an absent base
-- table or an absent view aborts by name rather than leaving a view owned by
-- the wrong role, because a silently skipped realignment is indistinguishable
-- from one that was never needed.

DO $$
DECLARE
    v_base_oid  oid := to_regclass('delegation_events');
    v_owner     name;
    v_view_name text;
    v_view_oid  oid;
    v_current   name;
BEGIN
    IF v_base_oid IS NULL THEN
        RAISE EXCEPTION
            'OMN-18159: delegation_events is not on the search_path, so the '
            'owner the aggregate views must be realigned to cannot be '
            'resolved. Refusing to guess: an aggregate view owned by a '
            'superuser bypasses FORCE ROW LEVEL SECURITY on every read.';
    END IF;

    SELECT pg_get_userbyid(relowner) INTO v_owner
    FROM pg_class WHERE oid = v_base_oid;

    FOREACH v_view_name IN ARRAY ARRAY[
        'projection_delegation_summary',
        'projection_delegation_model_routing',
        'projection_delegation_quality_gate',
        'projection_delegation_token_usage'
    ]
    LOOP
        v_view_oid := to_regclass(v_view_name);
        IF v_view_oid IS NULL THEN
            RAISE EXCEPTION
                'OMN-18159: aggregate view % is not on the search_path; '
                'migration 0039 creates all four, so its absence here means '
                'the corpus was applied partially.', v_view_name;
        END IF;

        SELECT pg_get_userbyid(relowner) INTO v_current
        FROM pg_class WHERE oid = v_view_oid;

        IF v_current IS DISTINCT FROM v_owner THEN
            EXECUTE format(
                'ALTER VIEW %s OWNER TO %I', v_view_oid::regclass::text, v_owner
            );
        END IF;
    END LOOP;
END
$$;
