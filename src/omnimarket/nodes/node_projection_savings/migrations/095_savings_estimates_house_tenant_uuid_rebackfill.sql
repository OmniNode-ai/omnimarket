-- OMN-19438: second house-slug re-attribution for savings_estimates.
--
-- THE DEFECT
--   091 moved every row stored under the house slug 'omninode' to the house
--   tenant's registry UUID. The lab applied it on 2026-09-25 at 04:18Z, about
--   28 hours before the fixed writer was live, so the unfixed writer then wrote
--   770 more rows under the slug (2026-09-25 04:23Z to 2026-09-26 07:36Z). The
--   migration runner records 091 as applied and never runs it again, so those
--   rows stay under the slug, invisible to every reader that binds the UUID, and
--   the ticket's third acceptance criterion (0 rows with tenant omninode) fails.
--
-- WHAT IT DOES
--   The same statement 091 ran, under a new migration id so the runner applies it
--   once more. The fixed writer is live, so nothing writes the slug any more and
--   this move is final. Every step of 091 holds here:
--     1. Resolves the house UUID (registry mirror row, else the pinned legacy
--        mapping) and aborts on registry drift.
--     2. UPDATEs every row whose tenant_id is exactly 'omninode'; no other value
--        is touched.
--     3. Sets the column DEFAULT to the UUID (already true after 091; restated so
--        a lane that skipped 091 ends in the same state).
--     4. Refuses to commit if any 'omninode' row remains.
--   A non-bypass migrator takes the table owner's role and one transaction-scoped
--   permissive policy, dropped before the block ends; the relation's RLS
--   enablement and forcing are never changed.
--
-- Idempotent: an apply that finds no slug rows moves nothing.

DO $$
DECLARE
  v_rel         REGCLASS;
  v_owner       REGROLE;
  v_bypass      BOOLEAN;
  v_house_uuid  TEXT := '820272f9-4aaf-5add-a2df-0af942852ab2';
  v_mirror_uuid TEXT;
  v_moved       BIGINT := 0;
  v_left        BIGINT := 0;
BEGIN
  v_rel := to_regclass('savings_estimates');
  IF v_rel IS NULL THEN
    RAISE NOTICE 'OMN-19438: savings_estimates absent; nothing to re-attribute';
    RETURN;
  END IF;

  -- to_regclass alone is not enough: on a lane where this migration's node
  -- (node_projection_savings) applies before node_projection_tenant_registry's
  -- own 0000_create_tenant_registry_mirror.sql (alphabetically "savings" sorts
  -- before "tenant_registry", and the corpus runner applies node dirs in that
  -- order), a pre-existing-but-not-yet-reconciled mirror can carry the table
  -- with none of its columns yet -- that 0000 file ADD COLUMNs tenant_uuid
  -- itself, guarded, for exactly this shape-drift class (OMN-15376). Reading
  -- tenant_uuid before it exists is not registry drift, it is "the mirror
  -- is not usable yet", the same case the table-absent branch above already
  -- handles by falling back to the pinned UUID with no drift check.
  IF to_regclass('tenant_registry_mirror') IS NOT NULL
     AND EXISTS (
       SELECT 1 FROM information_schema.columns
       WHERE table_name = 'tenant_registry_mirror' AND column_name = 'tenant_slug'
     )
     AND EXISTS (
       SELECT 1 FROM information_schema.columns
       WHERE table_name = 'tenant_registry_mirror' AND column_name = 'tenant_uuid'
     )
  THEN
    SELECT tenant_uuid::text INTO v_mirror_uuid
    FROM tenant_registry_mirror
    WHERE tenant_slug = 'omninode';
    IF v_mirror_uuid IS NOT NULL AND v_mirror_uuid <> v_house_uuid THEN
      RAISE EXCEPTION
        'OMN-19438: tenant registry drift for the house slug: '
        'tenant_registry_mirror records %, the pinned house UUID is %. '
        'Reconcile the registry before re-attributing any savings row.',
        v_mirror_uuid, v_house_uuid;
    END IF;
  END IF;

  SELECT c.relowner::regrole
  INTO v_owner
  FROM pg_catalog.pg_class c
  WHERE c.oid = v_rel;

  SELECT r.rolsuper OR r.rolbypassrls
  INTO v_bypass
  FROM pg_catalog.pg_roles r
  WHERE r.rolname = current_user;

  IF NOT v_bypass THEN
    IF NOT pg_has_role(current_user, v_owner, 'USAGE') THEN
      RAISE EXCEPTION
        'OMN-19438: % is not a member of savings_estimates owner role % with '
        'INHERIT, so under the forced tenant policy it would re-attribute '
        'zero rows and report success.',
        current_user, v_owner;
    END IF;
    IF NOT pg_has_role(current_user, v_owner, 'SET') THEN
      RAISE EXCEPTION
        'OMN-19438: % cannot SET ROLE to savings_estimates owner role %, so '
        'it cannot add the transaction-scoped re-attribution policy.',
        current_user, v_owner;
    END IF;
    PERFORM set_config('role', v_owner::text, true);
    DROP POLICY IF EXISTS omn19438_reattribute ON savings_estimates;
    CREATE POLICY omn19438_reattribute ON savings_estimates
      AS PERMISSIVE
      FOR ALL
      USING (tenant_id IN ('omninode', '820272f9-4aaf-5add-a2df-0af942852ab2'))
      WITH CHECK (tenant_id = '820272f9-4aaf-5add-a2df-0af942852ab2');
  END IF;

  UPDATE savings_estimates
  SET tenant_id = v_house_uuid
  WHERE tenant_id = 'omninode';
  GET DIAGNOSTICS v_moved = ROW_COUNT;

  ALTER TABLE savings_estimates
    ALTER COLUMN tenant_id SET DEFAULT '820272f9-4aaf-5add-a2df-0af942852ab2';

  -- Counted while the helper policy is still in place: under the forced
  -- tenant policy with no GUC set, a non-bypass owner would otherwise count
  -- zero rows whatever the table holds.
  SELECT count(*) INTO v_left
  FROM savings_estimates
  WHERE tenant_id = 'omninode';
  IF v_left <> 0 THEN
    RAISE EXCEPTION
      'OMN-19438: % savings_estimates rows still carry the house slug after '
      'the move; refusing to commit a partial re-attribution.',
      v_left;
  END IF;

  IF NOT v_bypass THEN
    DROP POLICY omn19438_reattribute ON savings_estimates;
  END IF;

  RAISE NOTICE
    'OMN-19438: savings_estimates house-tenant re-attribution: % rows moved '
    'from the slug to %',
    v_moved, v_house_uuid;
END;
$$;
