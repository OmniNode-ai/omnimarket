-- OMN-18774: generation_events sheds the tenant posture its own contract
-- forbids -- policy, row-level security, and the tenant_id column.
--
-- WHAT THIS CLOSES
-- generation_events is declared `schema: omninode_internal` by its owning
-- contract (node_projection_delegation/contract.yaml, db_io.db_tables). The
-- runtime resolves that declaration to InternalProjectionTableOperation
-- (omnibase_infra runtime/auto_wiring/handler_wiring.py), which REFUSES a
-- tenant_id key on the row and issues no set_config('app.tenant_id', ...) at
-- all. 0027's tenant_isolation policy compares tenant_id against
-- current_setting('app.tenant_id', true), so on the kernel path it is a
-- predicate no declared writer can satisfy, and the write survives only while
-- the connection owns the table and relforcerowsecurity is off.
--
-- On this relation the contradiction had already become load-bearing in the
-- CODE, not only in the DDL. OMN-16831 item 4 made the SYNC handler stamp the
-- attribution itself (handler_projection_delegation.py, house_tenant_write_
-- stamp), which is exactly the key the internal operation raises ValueError on
-- -- so the sync generation projection could not write on a runtime-kernel pod
-- at all. It went unobserved because the .201 dev lane runs the ASYNC runner,
-- whose raw INSERT named tenant_id as $24 and bound the GUC for the statement
-- (OMN-15919). Two twins, diverged, only one of them able to run. The change
-- that lands with this migration removes the stamp from BOTH, so the relation's
-- writers and its schema express the same classification.
--
-- THE DIRECTION WAS ALREADY RULED
-- The OMN-14894 authorization row -- docs/tracking/ROLLING_WORK_LEDGER.md:654,
-- OPERATOR-CONSENT, approved_by=operator, 2026-09-14T12:11:31Z -- puts the
-- answer in its own OUT OF SCOPE list, verbatim: "relations classified
-- OMNINODE_INTERNAL or ambiguous". Internal-classified relations receive no
-- tenant stamping and no row-level security. 0027 (OMN-14974) predates the
-- classification manifest; its posture is pre-classification residue, not a
-- deferred capability.
--
-- WHY THE COLUMN IS DROPPED RATHER THAN KEPT AS PROVENANCE
-- A kept-but-unstamped column has no coherent shape. NOT NULL with no DEFAULT
-- refuses every kernel-path insert, because the operation class rejects the
-- only key that could fill it. NULLABLE with no DEFAULT is a column that is
-- always NULL. The platform already has a provenance field for internal
-- relations that want one (source_tenant_id); nothing here supplies it, so
-- nothing here invents it.
--
-- NO ATTRIBUTION IS LOST. Every stored row carries the single house constant
-- 'omninode' -- recorded by house_tenant_write_stamp on the async path and by
-- the DDL DEFAULT before that, and in neither case naming a tenant that
-- submitted anything. The house stamp resolves to that constant precisely
-- WHENEVER the lane configures no Settings.onex_tenant_id, which is what a
-- customer deployment configures, so the column's content is a constant of the
-- schema rather than a fact about a workload.
--
-- THIS DOES NOT TOUCH THE TENANT-CLASSIFIED RELATIONS
-- delegation_events, delegation_judge_verdict_events, delegation_budget_state
-- and the rest of this node's TENANT-declared surface keep their tenant_id,
-- their policies and their RLS. The house-tenant ruling is unchanged for them
-- (OMN-17422 owns that half). What changes here is one relation whose declared
-- domain says it was never tenant data.
--
-- READ BLAST RADIUS, STATED
-- app_dashboard and role_omnidash keep their table-level grants and now see
-- every row rather than the rows matching a session GUC. For a relation the
-- ruling declares un-partitioned by tenant, that IS the posture.
--
-- ONE TRANSACTION, DELIBERATELY
-- The forward runner is `psql -v ON_ERROR_STOP=1 -f <file>` with no
-- --single-transaction, so each top-level statement commits on its own.
-- Between a committed DROP POLICY and a not-yet-run DISABLE ROW LEVEL SECURITY
-- the relation would sit with RLS on and zero policies -- every application
-- read denied until someone re-ran the file. That is the OMN-17288 defect
-- exactly; the DO block makes the three statements one commit.
--
-- SCHEMA-QUALIFICATION: bare `public`, matching 0027 and every other migration
-- in this chain. generation_events lives physically in public on every real
-- lane.
DO $drop_generation_tenant_posture$
BEGIN
    IF to_regclass('public.generation_events') IS NULL THEN
        RAISE NOTICE
            'OMN-18774: generation_events is absent on this lane; the '
            'tenant-posture removal is a no-op';
        RETURN;
    END IF;

    -- Policy first: it references tenant_id, so the column cannot be dropped
    -- while it stands.
    DROP POLICY IF EXISTS tenant_isolation ON public.generation_events;
    ALTER TABLE public.generation_events DISABLE ROW LEVEL SECURITY;
    ALTER TABLE public.generation_events DROP COLUMN IF EXISTS tenant_id;
END
$drop_generation_tenant_posture$;
