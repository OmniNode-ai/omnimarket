-- OMN-15732 AC2 (fix for OMN-15356's original 0003): define the fail-closed
-- tenant slug->UUID mapping function as its OWN qualified, ownership-declared
-- application object, split out of what was originally one file together
-- with the capability_scores column conversion (now 0004).
--
-- WHY THIS IS ITS OWN FILE, NOT INLINED IN 0004
--   omnibase_infra's application-database-domain-sql gate
--   (scripts/ci/check_application_database_sql.py +
--   validation/application_database_domain_enforcement.py) enforces two
--   independent things per changed SQL file: (a) every application relation
--   target must be schema-qualified and out of `public`, and (b) every
--   NEWLY CREATED application object must carry exactly one ownership
--   declaration in a checked-in manifest. `public.capability_scores` itself
--   is a PRE-EXISTING relation that intentionally stays unqualified in
--   `public` until the governed OMN-15359 tenant/internal schema cutover
--   moves the whole classified-TENANT relation set at once -- that decision
--   is explicitly OUT OF SCOPE here and is why 0002 (and, before this split,
--   0003) needed the file-level "legacy default schema" exemption in infra's
--   checker. But `house_tenant_map_slug_to_uuid` is a BRAND NEW object with
--   no such precedent: OMN-15732 AC2 requires it can never permanently
--   escape ownership declaration by riding along inside an exempted file.
--   The checker's exemption is file-granular (skip-or-lint-the-whole-file),
--   so the only way to get this function fully linted and ownership-checked
--   while leaving the pre-existing, deferred capability_scores exemption
--   untouched is to physically separate the two into different files. This
--   file is NOT exempted and is fully linted; 0004 keeps the existing
--   exemption for the table it still touches.
--
-- WHY platform_catalog, NOT tenant OR omninode_internal
--   `docs/evidence/OMN-15423-relation-inventory.json` documents this
--   function's eventual target_schema as `tenant` (same governed OMN-15359
--   cutover as capability_scores itself) -- that target is UNCHANGED and
--   deferred by this PR, not overridden. `tenant` needs the same OMN-15359
--   decision + tenant-schema bootstrap this PR explicitly avoids requiring.
--   Between the two schemas that need neither: `omninode_internal` is
--   created ONLY by the standalone OMN-15420 cutover repository initializer
--   (src/omnibase_infra/migration/cutover/sql/bootstrap.sql, whose own
--   header says "not part of the forward migration stream") -- there is no
--   `CREATE SCHEMA omninode_internal` anywhere under
--   omnibase_infra/docker/migrations/forward/, so a fresh env running only
--   the forward-migration runner has no guarantee that schema exists.
--   `platform_catalog` IS guaranteed: omnibase_infra's
--   `scripts/run-forward-migrations.sh` calls `prepare_canonical_ledger`
--   (which sources `docker/migrations/forward/_ledger/bootstrap.sql`,
--   containing `CREATE SCHEMA IF NOT EXISTS platform_catalog;`) BEFORE the
--   node-migration discovery loop that would ever apply this file --
--   platform_catalog is a hard prerequisite of the ledger the runner itself
--   uses to track whether ANY node migration has been applied. A closed,
--   static, platform-wide reference mapping (never per-tenant data, never
--   raw internal runtime bookkeeping) also fits platform_catalog's own
--   domain semantics reasonably as an interim home. This is an INTERIM
--   placement: OMN-15359 may later move this function into `tenant`
--   alongside capability_scores in one governed cutover; that move is not
--   this PR's job.
--
-- The defensive `CREATE SCHEMA IF NOT EXISTS` below is a no-op given the
-- ledger-ordering guarantee above; it is kept because a Docker proof harness
-- may apply this file directly outside the full forward-migration runner,
-- and the statement is idempotent either way.
--
-- Idempotent: CREATE OR REPLACE, so a second application is a no-op.

CREATE SCHEMA IF NOT EXISTS platform_catalog;

CREATE OR REPLACE FUNCTION platform_catalog.house_tenant_map_slug_to_uuid(p_value TEXT)
RETURNS UUID
LANGUAGE plpgsql
IMMUTABLE
AS $$
BEGIN
    IF p_value = 'omninode' THEN
        RETURN '820272f9-4aaf-5add-a2df-0af942852ab2'::uuid;
    END IF;
    RAISE EXCEPTION
        'OMN-15356: no canonical UUID mapping for tenant value % -- refusing '
        'to invent or default one; extend house_tenant_map_slug_to_uuid only '
        'after confirming this is a real, reviewed tenant identity',
        p_value;
END;
$$;
