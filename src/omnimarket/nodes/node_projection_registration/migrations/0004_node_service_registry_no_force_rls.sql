-- OMN-15336 item 4 (required-fix #4) / operator ruling R-q (2026-08-05,
-- rolling ledger 23:45Z): registry and orchestration state
-- (node_service_registry) is omninode_internal runtime state, not tenant
-- data. FORCE ROW LEVEL SECURITY on internal-domain runtime state is the
-- wrong posture: it forces EVERY reader, including the table owner, through
-- a tenant_id predicate that internal registry rows do not carry
-- meaningfully (0002 stamped a DEFAULT 'omninode' single-tenant value —
-- there is no real multi-tenant partition of "which node is running where").
--
-- This reverses ONLY the FORCE half of 0002_node_service_registry_tenant_rls
-- (leaves RLS ENABLEd and the tenant_isolation POLICY in place, so a future
-- tenant-scoped consumer is not blocked from reintroducing FORCE explicitly
-- with a considered ruling of its own) — narrower than DISABLE ROW LEVEL
-- SECURITY or DROP POLICY, matching item 4's own recommendation "(b) strip
-- FORCE-RLS" precisely rather than removing the RLS posture wholesale.
--
-- Idempotent on both paths, matching 0003's own precedent: the registration
-- trio (0000/0001/0002) is operator-fenced by default (OMN-15379/OMN-15349)
-- and released only on the compose dev lane and the k8s onex-dev Job
-- (ruling 21) — a fresh, still-fenced database has no node_service_registry
-- table at all, and this migration must be a deterministic no-op there, not
-- an error.
--
-- Does NOT need a fence-manifest entry: the OMN-15336 item-4 guard
-- (migration_declares_unclassified_force_rls in run-forward-migrations.sh)
-- only refuses migrations that ENABLE FORCE ROW LEVEL SECURITY without a
-- fence entry. `NO FORCE ROW LEVEL SECURITY` is the disabling form and is
-- explicitly excluded from that match — the guard exists to gate the
-- hazard this migration removes, not to block its own remedy.
--
-- Reconciles with ruling 15 (OMN-15379, "the registration trio moves
-- together") and ruling 21 (OMN-15332 comment 1a067542, the k8s durable
-- release): neither ruling is reversed by this migration. Both rulings
-- govern whether 0000/0001/0002 apply at all (fence membership); this
-- migration only changes the FORCE posture of a table that has already been
-- released and created, and ships unfenced so it applies on every lane
-- where the trio has already landed, converging them to the corrected
-- posture without re-litigating the release decision itself.
--
-- Ticket: OMN-15336 (item 4). Introduced-by: OMN-15343 (0002's FORCE).
-- Domain corroboration: node_projection_registration/contract.yaml
-- db_io.schema=omninode_internal; 2026-08-02 operator domain ruling
-- ("registry... stays omninode_internal"); OMN-15656's landed
-- grants-derivation correction ("node_service_registry/live_events are
-- internal not tenant"); R-q's generalized precedent ("FORCE-RLS on
-- internal-domain runtime state is wrong").
DO $unforce_registry_rls$
BEGIN
  IF to_regclass('public.node_service_registry') IS NOT NULL THEN
    ALTER TABLE public.node_service_registry NO FORCE ROW LEVEL SECURITY;
  ELSE
    RAISE NOTICE
      'node_service_registry is absent; FORCE-RLS reversal is a no-op '
      '(the registration trio is still fenced on this database)';
  END IF;
END
$unforce_registry_rls$;
