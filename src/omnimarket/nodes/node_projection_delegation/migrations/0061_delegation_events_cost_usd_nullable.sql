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
-- The backfill runs under FORCE ROW LEVEL SECURITY (0023). A non-superuser
-- owner sees no row without a tenant GUC, so an UPDATE would match zero rows
-- and report success. As 0033 does, the block assumes the owner role, lifts
-- FORCE for the one statement inside this transaction and restores it before
-- COMMIT, so no committed state ever has FORCE off.

BEGIN;

ALTER TABLE delegation_events
    ALTER COLUMN cost_usd DROP NOT NULL,
    ALTER COLUMN cost_usd DROP DEFAULT;

DO $$
DECLARE
    v_owner  NAME;
    v_forced BOOLEAN;
BEGIN
    v_owner := (
        SELECT pg_get_userbyid(relowner)
        FROM pg_catalog.pg_class
        WHERE oid = 'delegation_events'::regclass);
    v_forced := (
        SELECT relforcerowsecurity
        FROM pg_catalog.pg_class
        WHERE oid = 'delegation_events'::regclass);

    IF NOT pg_has_role(current_user, v_owner, 'USAGE') THEN
        RAISE EXCEPTION
            'OMN-20717: the migrate identity % is not a member of '
            'delegation_events'' owner role %; under FORCE ROW LEVEL SECURITY '
            'the backfill would see no row and silently change nothing.',
            current_user, v_owner;
    END IF;
    PERFORM set_config('role', v_owner::text, true);

    IF v_forced THEN
        ALTER TABLE delegation_events NO FORCE ROW LEVEL SECURITY;
    END IF;

    UPDATE delegation_events
    SET cost_usd = NULL
    WHERE cost_measurement_source = 'no_cost_model'
      AND cost_usd = 0;

    IF v_forced THEN
        ALTER TABLE delegation_events FORCE ROW LEVEL SECURITY;
    END IF;
END;
$$;

COMMIT;
