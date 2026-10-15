-- OMN-20717: delegation_events.cost_usd may be NULL, and NULL means "this call
-- was not priced", never zero.
--
-- 0007 declared cost_usd NUMERIC NOT NULL DEFAULT 0, so an unpriced call was
-- stored, summed and served as 0 -- indistinguishable from a call that was
-- priced and really cost nothing (a free local model). Ruling (2026-10-14):
-- the column holds NULL for an unpriced call; existing rows stay as they are
-- unless the row's own pricing source says it was unpriced.
--
-- 1. Drop the NOT NULL and the DEFAULT. A row that names no cost (the
--    verdict-first partial upsert) now stores NULL rather than an invented 0.
-- 2. Backfill: only rows whose cost_measurement_source (0018) is
--    'no_cost_model' AND whose cost_usd is 0 become NULL. A zero with any other
--    source, or no source, is left alone: an old 0 cannot be told apart from a
--    free call, so it is not rewritten.
--
-- The table's row-level security is forced (0023): a migrate identity that is
-- neither superuser nor BYPASSRLS sees no row without a tenant GUC, so the
-- UPDATE would match nothing and report success. As the savings rebackfill
-- (095) does, such an identity takes the table owner's role and adds one
-- transaction-scoped permissive policy covering exactly the rows the backfill
-- may touch, and drops it before the block ends. The table's row-level
-- security, its forcing and tenant_isolation are never changed.

BEGIN;

ALTER TABLE delegation_events
    ALTER COLUMN cost_usd DROP NOT NULL,
    ALTER COLUMN cost_usd DROP DEFAULT;

DO $$
DECLARE
    v_owner  REGROLE;
    v_bypass BOOLEAN;
BEGIN
    v_owner := (
        SELECT c.relowner::regrole
        FROM pg_catalog.pg_class c
        WHERE c.oid = 'delegation_events'::regclass);
    v_bypass := (
        SELECT r.rolsuper OR r.rolbypassrls
        FROM pg_catalog.pg_roles r
        WHERE r.rolname = current_user);

    IF NOT v_bypass THEN
        IF NOT pg_has_role(current_user, v_owner, 'USAGE') THEN
            RAISE EXCEPTION
                'OMN-20717: % is not a member of delegation_events'' owner '
                'role % with INHERIT, so under the forced tenant policy the '
                'backfill would see no row and silently change nothing.',
                current_user, v_owner;
        END IF;
        -- INHERIT and SET are independent memberships since PostgreSQL 16
        -- (OMN-17316): without SET, set_config('role', ...) below aborts on a
        -- bare permission error.
        IF NOT pg_has_role(current_user, v_owner, 'SET') THEN
            RAISE EXCEPTION
                'OMN-20717: % cannot SET ROLE to delegation_events'' owner '
                'role %, so it cannot add the transaction-scoped backfill '
                'policy. Fix: GRANT % TO % WITH SET TRUE.',
                current_user, v_owner, v_owner, current_user;
        END IF;
        PERFORM set_config('role', v_owner::text, true);
        DROP POLICY IF EXISTS omn20717_unpriced_backfill ON delegation_events;
        CREATE POLICY omn20717_unpriced_backfill ON delegation_events
          AS PERMISSIVE
          FOR ALL
          USING (cost_measurement_source = 'no_cost_model')
          WITH CHECK (cost_measurement_source = 'no_cost_model');
    END IF;

    UPDATE delegation_events
    SET cost_usd = NULL
    WHERE cost_measurement_source = 'no_cost_model'
      AND cost_usd = 0;

    IF NOT v_bypass THEN
        DROP POLICY omn20717_unpriced_backfill ON delegation_events;
    END IF;
END;
$$;

COMMIT;
