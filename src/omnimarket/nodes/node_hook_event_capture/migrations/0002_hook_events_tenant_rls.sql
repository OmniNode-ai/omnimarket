-- OMN-16090: tenant isolation for hook_events.
--
-- SPLIT OUT OF 0001 DELIBERATELY, AND FENCED.
--   scripts/run-forward-migrations.sh refuses ANY new node migration applying
--   FORCE ROW LEVEL SECURITY unless its id appears in
--   docker/migrations/forward/fenced-node-migrations.yaml. That refusal is
--   correct and this file does not try to dodge it: the id is added to that
--   manifest in the same change that vendors this file, so the runner SKIPS
--   this migration and records the skip (never inserting a schema_migrations
--   row, so a later un-fence is not a silent no-op).
--
--   Consequence, stated plainly: hook_events is created WITHOUT row-level
--   security until an operator releases this id. That is the same posture
--   every other tenant-classified relation on this surface currently has --
--   the delegation quartet, registration 0002 and
--   delegation_inference_response 0003 are all fenced today, so none of them
--   enforces RLS on a live lane either.
--
-- WHY THE FENCE EXISTS, AND WHY THIS NODE IS A CLEANER UN-FENCE CANDIDATE
--   The stated reason those ids are held (fenced-node-migrations.yaml,
--   verbatim) is OMN-15301: "the projection writer never sets app.tenant_id
--   per connection", so an un-gated FORCE apply produces a false-clean write
--   lockout -- zero rows written, no error raised.
--
--   That specific hazard does NOT apply to this node's writer. Its handler
--   passes `tenant=tenant_id` explicitly to AsyncpgAdapter.execute(), which
--   issues `set_config('app.tenant_id', <tenant>, true)` on the SAME
--   connection inside the SAME transaction as the INSERT, using the tenant
--   value the row itself carries (the OMN-15919 shape, adopted precisely to
--   avoid the read-path/write-path resolver split that produced "new row
--   violates row-level security policy" on the delegation writes).
--
--   That is the bar the manifest names for un-gating: "un-gate only in a
--   change that also proves the writer sets app.tenant_id per connection."
--   This node meets it. The release is still NOT taken here, because a fence
--   release is a per-lane operator decision with live-data consequences, not
--   a call the change that introduces the table gets to make for itself.
--
-- UN-FENCE CONDITION
--   Remove this id from fenced-node-migrations.yaml in a change that also
--   updates the pinned manifest test, and that verifies on the target lane
--   that a write through node_hook_event_capture lands a row (not a
--   false-clean zero-row apply) with the policy active.
--
-- Fail-closed: current_setting('app.tenant_id', true) is NULL when the GUC is
-- unset, the predicate is NULL, and ZERO rows are visible. No default-tenant
-- fallback, by design.
--
-- BLAST RADIUS -- FORCE constrains the table OWNER too, but a writer connected
-- as the postgres SUPERUSER (the compose lanes) bypasses RLS regardless. The
-- real isolation boundary is this policy PLUS a non-superuser, NOBYPASSRLS
-- writer role (OMN-14899 / OMN-15425).
--
-- PHYSICAL SCHEMA IS NOT MOVED HERE -- see OMN-15359. Every relation already
-- classified TENANT is still physically in `public`; relocating this one alone
-- would create the split that the target/current inventory exists to prevent.
--
-- Idempotent: ENABLE/FORCE are idempotent, the policy is DROP + CREATE, the
-- GRANTs are idempotent.

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_roles
    WHERE rolname = 'app_dashboard'
      AND NOT rolsuper
      AND NOT rolbypassrls
  ) THEN
    RAISE EXCEPTION
      'app_dashboard role missing or RLS-bypassing - apply omnibase_infra forward migration '
      '094_create_app_dashboard_role.sql (OMN-14899) before this RLS '
      'migration. RLS grants without the constrained read role are the '
      'exact bypass this work exists to prevent.';
  END IF;
END;
$$;

GRANT USAGE ON SCHEMA public TO app_dashboard;

ALTER TABLE public.hook_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.hook_events FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tenant_isolation ON public.hook_events;
CREATE POLICY tenant_isolation ON public.hook_events
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

-- Read grant for the constrained dashboard reader. RLS still filters every row
-- this role can see -- the grant is what makes the RLS-scoped read path
-- reachable at all, not a widening of it.
--
-- This grant is REQUIRED, not optional, and not a judgement call: the
-- OMN-14894 ratchet (tests/unit/projection/
-- test_tenant_isolation_migrations_grant_app_dashboard_select_omn14894.py)
-- asserts that every migration creating a `tenant_isolation` policy also
-- carries the sibling app_dashboard SELECT grant IN THE SAME FILE. The first
-- draft of this migration omitted it on the reasoning that no dashboard reader
-- consumes hook_events yet; the ratchet rejected that, and the ratchet is
-- right -- a policy landing without its paired grant is how a relation ends up
-- RLS-protected but unreadable by the only constrained role that should ever
-- read it, discovered later at query time rather than here.
GRANT SELECT ON public.hook_events TO app_dashboard;
