-- OMN-15655 / operator ruling 2026-08-02 (house tenant): re-key
-- delegation_budget_state rows written under the orphan tenant 'default'.
--
-- THE DEFECT
--   `handler_budget_state.py` carried `DEFAULT_TENANT = "default"` while every
--   other writer on this surface resolves to `"omninode"` and every landed
--   column DEFAULT is `'omninode'` (delegation 0022/0025, savings 080,
--   registration 0002, inference-response 0002, omnidash 0001_tenant_rls).
--   `delegation_budget_state.tenant_id` is `TEXT NOT NULL` with NO column
--   default (migration 0019), so that handler constant was the ONLY thing
--   deciding where an unattributed budget row landed -- and it landed them
--   under a tenant id that appears in no registry, no RLS policy and no other
--   relation. The ruling settles it: there is exactly ONE house tenant.
--
--   The handler constant is fixed in the same change. This migration moves the
--   rows it already wrote, so the pre-existing budget state is not orphaned
--   from the tenant the writer will use from now on.
--
-- COLLISION HANDLING -- NOT A BLIND UPDATE
--   `delegation_budget_state` is unique on (tenant_id, cost_tier_name,
--   budget_period) (0019). A blind `UPDATE ... SET tenant_id = 'omninode'`
--   raises a unique violation whenever BOTH a 'default' row and an 'omninode'
--   row exist for the same (tier, period). Those pairs are two divergent
--   accumulated states for one budget window; silently picking one, or summing
--   them, would invent a number nobody measured. So:
--     * non-colliding rows are moved,
--     * colliding rows are LEFT IN PLACE under 'default' and reported via
--       RAISE NOTICE with their keys, so the residual is visible and
--       hand-resolvable rather than silently destroyed or silently merged.
--   Expected count in every known lane is zero: the 'default' constant was
--   only ever reachable for a budgeted-tier event that carried no tenant.
--
-- Idempotent: re-running moves nothing on the second pass (no 'default' rows
-- remain except the reported collisions, and those are re-reported, not
-- re-attempted).

DO $$
DECLARE
  v_moved   BIGINT := 0;
  v_blocked BIGINT := 0;
  v_row     RECORD;
  v_state_relation REGCLASS;
BEGIN
  v_state_relation := to_regclass('delegation_budget_state');
  IF v_state_relation IS NULL THEN
    RAISE NOTICE 'delegation_budget_state absent; nothing to re-key';
    RETURN;
  END IF;
  IF NOT EXISTS (
    SELECT 1
    FROM pg_class
    WHERE oid = v_state_relation
      AND relkind IN ('r', 'p')
  ) THEN
    RAISE EXCEPTION
      'OMN-15655: delegation_budget_state resolves to %, not a table-like relation',
      v_state_relation;
  END IF;

  WITH moved AS (
    UPDATE delegation_budget_state AS src
    SET tenant_id = 'omninode'
    WHERE src.tenant_id = 'default'
      AND NOT EXISTS (
        SELECT 1
        FROM delegation_budget_state AS dst
        WHERE dst.tenant_id = 'omninode'
          AND dst.cost_tier_name = src.cost_tier_name
          AND dst.budget_period = src.budget_period
      )
    RETURNING 1
  )
  SELECT count(*) INTO v_moved FROM moved;

  FOR v_row IN
    SELECT cost_tier_name, budget_period
    FROM delegation_budget_state
    WHERE tenant_id = 'default'
  LOOP
    v_blocked := v_blocked + 1;
    RAISE NOTICE
      'delegation_budget_state: tenant ''default'' row left in place; an '
      '''omninode'' row already exists for (cost_tier_name=%, budget_period=%) '
      '-- two divergent states for one budget window, resolve by hand',
      v_row.cost_tier_name, v_row.budget_period;
  END LOOP;

  RAISE NOTICE
    'delegation_budget_state house-tenant re-key: % moved, % left as collisions',
    v_moved, v_blocked;
END;
$$;
