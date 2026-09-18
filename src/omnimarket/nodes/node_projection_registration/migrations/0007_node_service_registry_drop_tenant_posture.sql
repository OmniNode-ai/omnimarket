-- OMN-18774: node_service_registry sheds the tenant posture its own contract
-- forbids -- policy, row-level security, and the tenant_id column.
--
-- WHAT THIS CLOSES
-- node_service_registry is declared `schema: omninode_internal` by its owning
-- contract (node_projection_registration/contract.yaml, db_io.db_tables). The
-- runtime resolves that declaration to InternalProjectionTableOperation
-- (omnibase_infra runtime/auto_wiring/handler_wiring.py), which REFUSES a
-- tenant_id key on the row and issues no set_config('app.tenant_id', ...) at
-- all. The live async writer agrees: its INSERT names nine columns and
-- tenant_id is not one of them (handlers/handler_registration.py), and it
-- passes no tenant, so the statement opens no tenant transaction either.
--
-- So 0002's tenant_isolation policy, which compares tenant_id against
-- current_setting('app.tenant_id', true), is a predicate NO DECLARED WRITER
-- CAN EVER SATISFY. Every write succeeds today only because the connection
-- owns the table and relforcerowsecurity is off, which exempts the session.
-- The moment the runtime connects as the non-bypassing login dev-lane security
-- parity requires (OMN-18256 AC1/AC2), every write to this relation is refused.
--
-- And because the policy could not be satisfied, the DDL authored the
-- attribution instead: `tenant_id text NOT NULL DEFAULT 'omninode'` stamped the
-- house tenant constant onto 100 per cent of rows, by a mechanism no writer can
-- see and no reader can distinguish from a deliberate attribution.
--
-- THE DIRECTION WAS ALREADY RULED
-- The OMN-14894 authorization row -- docs/tracking/ROLLING_WORK_LEDGER.md:654,
-- OPERATOR-CONSENT, approved_by=operator, 2026-09-14T12:11:31Z -- puts the
-- answer in its own OUT OF SCOPE list, verbatim: "relations classified
-- OMNINODE_INTERNAL or ambiguous". Internal-classified relations receive no
-- tenant stamping and no row-level security.
--
-- This is the second half of a reversal that was already begun on exactly this
-- reasoning. 0004_node_service_registry_no_force_rls (OMN-15336 item 4,
-- operator ruling R-q 2026-08-05) removed the FORCE half and said why:
-- "registry and orchestration state is omninode_internal runtime state, not
-- tenant data ... there is no real multi-tenant partition of which node is
-- running where". It deliberately left ENABLE and the policy standing, so a
-- considered ruling could reintroduce FORCE later. The 2026-09-14 ruling is
-- that considered ruling, and it goes the other way. This file finishes it.
--
-- WHY THE COLUMN IS DROPPED RATHER THAN KEPT AS PROVENANCE
-- A kept-but-unstamped column has no coherent shape. NOT NULL with no DEFAULT
-- refuses every kernel-path insert, because the operation class rejects the
-- only key that could fill it. NULLABLE with no DEFAULT is a column that is
-- always NULL -- dead weight that reads to any future auditor as an attribution
-- surface that was abandoned rather than one that was never owed. The platform
-- already has a provenance field for internal relations that want one
-- (source_tenant_id, which InternalProjectionTableOperation admits and refuses
-- as a conflict key); nothing here supplies it, so nothing here invents it.
--
-- NO ATTRIBUTION IS LOST. Every stored row carries the single house constant
-- 'omninode', authored by this DEFAULT and by nothing else. Dropping the column
-- discards a value the database invented, not one a writer recorded.
--
-- READ BLAST RADIUS, STATED
-- app_dashboard keeps its table-level SELECT grant and now sees every row
-- rather than the rows matching its session GUC. For a relation the ruling
-- declares un-partitioned by tenant, that IS the posture -- the previous
-- narrowing was a partition that did not exist, enforced against a value the
-- schema had invented.
--
-- ONE TRANSACTION, DELIBERATELY
-- The forward runner is `psql -v ON_ERROR_STOP=1 -f <file>` with no
-- --single-transaction, so each top-level statement commits on its own. Between
-- a committed DROP POLICY and a not-yet-run DISABLE ROW LEVEL SECURITY the
-- relation would sit with RLS on and zero policies -- every application read
-- denied, permanently, until someone re-ran the file. That is the OMN-17288
-- defect exactly; the DO block makes the three statements one commit.
--
-- IDEMPOTENT, AND A NO-OP ON A LANE WITHOUT THE TABLE
-- The registration trio (0000/0001/0002) is operator-fenced by default
-- (OMN-15379/OMN-15349) and released only on the compose dev lane and the k8s
-- onex-dev Job, so a still-fenced database has no node_service_registry at all.
-- This file must be a deterministic no-op there, not an error -- the same
-- posture 0003 and 0004 take. It ships UNFENCED for the same reason 0004 did:
-- the fence guard refuses migrations that ENABLE FORCE ROW LEVEL SECURITY
-- without a fence entry, and this file is the disabling form of that hazard.
DO $drop_registry_tenant_posture$
BEGIN
    IF to_regclass('public.node_service_registry') IS NULL THEN
        RAISE NOTICE
            'OMN-18774: node_service_registry is absent; the tenant-posture '
            'removal is a no-op (the registration trio is still fenced on '
            'this database)';
        RETURN;
    END IF;

    -- Policy first: it references tenant_id, so the column cannot be dropped
    -- while it stands.
    DROP POLICY IF EXISTS tenant_isolation ON public.node_service_registry;
    ALTER TABLE public.node_service_registry DISABLE ROW LEVEL SECURITY;
    ALTER TABLE public.node_service_registry DROP COLUMN IF EXISTS tenant_id;
END
$drop_registry_tenant_posture$;
