-- OMN-19438: re-attribute savings_estimates rows stored under the house SLUG
-- to the house tenant's canonical UUID, and make the UUID the column default.
--
-- THE DEFECT
--   Both savings writers stamped a row that recorded no tenant through
--   house_tenant_write_stamp, which answers in the column's representation.
--   savings_estimates.tenant_id is TEXT (080), so it answered with the slug
--   'omninode', and the column DEFAULT is the same slug. Every reader binds
--   the UUID (the tenant savings endpoint binds str(uuid) against this TEXT
--   column), so none of those rows is visible to the tenant they belong to.
--   Measured on the .201 dev lane 2026-09-24: 5,582 rows under 'omninode'
--   against 99 under the house UUID.
--
--   The writers are fixed first (they resolve the house tenant through the
--   registry and stamp its UUID). This migration then moves the rows they
--   already wrote. It must be applied only after the fixed writer is live, or
--   rows written in between land under the slug again.
--
-- WHAT IT DOES
--   1. Resolves the house UUID: the tenant_registry_mirror row for the slug
--      when the mirror holds one, else the pinned house UUID (the closed
--      legacy mapping, uuid5(NAMESPACE_DNS, 'house-tenant.omninode.ai')).
--      A mirror row that disagrees with the pinned value is registry drift and
--      aborts the transaction rather than picking one.
--   2. UPDATEs every row whose tenant_id is exactly 'omninode'. No other value
--      is touched. savings_estimates is unique on (session_id,
--      event_timestamp, model_local, model_cloud_baseline), which does not
--      include tenant_id, so the move cannot collide.
--   3. Sets the column DEFAULT to the UUID, so an insert that omits the column
--      can no longer reintroduce the slug.
--   4. Refuses to commit if any 'omninode' row remains.
--
-- ROW LEVEL SECURITY
--   081 ENABLEs and FORCEs RLS with a policy comparing tenant_id to the
--   app.tenant_id GUC. A superuser or BYPASSRLS migrator is not subject to it.
--   Any other migrator must hold the table owner's role with both INHERIT and
--   SET: the block takes the owner's role for the transaction and lifts FORCE
--   for the move (an owner is exempt from a policy that is not FORCEd), then
--   restores FORCE before the block ends. Under FORCE an UPDATE that changes
--   tenant_id cannot satisfy USING and WITH CHECK with one GUC value, and a
--   blind UPDATE would silently match zero rows. 081's tenant_isolation policy
--   is restated verbatim inside the same block, so the file never commits the
--   relation enforcing RLS without it.
--
-- Idempotent: a second apply finds no slug rows and moves nothing.

DO $$
DECLARE
  v_rel         REGCLASS;
  v_owner       REGROLE;
  v_forced      BOOLEAN;
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

  IF to_regclass('tenant_registry_mirror') IS NOT NULL THEN
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

  SELECT c.relowner::regrole, c.relforcerowsecurity
  INTO v_owner, v_forced
  FROM pg_class c
  WHERE c.oid = v_rel;

  SELECT r.rolsuper OR r.rolbypassrls
  INTO v_bypass
  FROM pg_roles r
  WHERE r.rolname = current_user;

  IF NOT v_bypass THEN
    IF NOT pg_has_role(current_user, v_owner, 'USAGE') THEN
      RAISE EXCEPTION
        'OMN-19438: % is not a member of savings_estimates owner role % with '
        'INHERIT, so under FORCE ROW LEVEL SECURITY it would re-attribute '
        'zero rows and report success.',
        current_user, v_owner;
    END IF;
    IF NOT pg_has_role(current_user, v_owner, 'SET') THEN
      RAISE EXCEPTION
        'OMN-19438: % cannot SET ROLE to savings_estimates owner role %, so '
        'it cannot lift FORCE ROW LEVEL SECURITY for the move.',
        current_user, v_owner;
    END IF;
    PERFORM set_config('role', v_owner::text, true);
    IF v_forced THEN
      ALTER TABLE savings_estimates NO FORCE ROW LEVEL SECURITY;
    END IF;
  END IF;

  WITH moved AS (
    UPDATE savings_estimates
    SET tenant_id = v_house_uuid
    WHERE tenant_id = 'omninode'
    RETURNING 1
  )
  SELECT count(*) INTO v_moved FROM moved;

  ALTER TABLE savings_estimates
    ALTER COLUMN tenant_id SET DEFAULT '820272f9-4aaf-5add-a2df-0af942852ab2';

  -- Counted BEFORE FORCE is restored: under FORCE with no GUC set a
  -- non-bypass owner would count zero rows whatever the table holds.
  SELECT count(*) INTO v_left
  FROM savings_estimates
  WHERE tenant_id = 'omninode';
  IF v_left <> 0 THEN
    RAISE EXCEPTION
      'OMN-19438: % savings_estimates rows still carry the house slug after '
      'the move; refusing to commit a partial re-attribution.',
      v_left;
  END IF;

  IF NOT v_bypass AND v_forced THEN
    ALTER TABLE savings_estimates FORCE ROW LEVEL SECURITY;
  END IF;

  -- 081's policy, restated verbatim inside this same block, so no path
  -- through this file commits the relation enforcing RLS with no policy.
  DROP POLICY IF EXISTS tenant_isolation ON savings_estimates;
  CREATE POLICY tenant_isolation ON savings_estimates
    FOR ALL
    USING (tenant_id = current_setting('app.tenant_id', true))
    WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

  RAISE NOTICE
    'OMN-19438: savings_estimates house-tenant re-attribution: % rows moved '
    'from the slug to %',
    v_moved, v_house_uuid;
END;
$$;
