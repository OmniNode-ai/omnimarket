-- OMN-19988: FORCE ROW LEVEL SECURITY for local_environment.
--
-- Split out of 0000 deliberately, and fenced, as the other tenant projections
-- do: the forward-migration runner refuses a new node migration applying FORCE
-- ROW LEVEL SECURITY unless its id is listed in the fenced-node-migrations
-- manifest, and fencing the file that creates the table would skip the CREATE
-- TABLE and break the 0001 grants on a fresh database. Until the fence lifts,
-- the table carries ENABLE ROW LEVEL SECURITY and its tenant_isolation policy
-- from 0000.
--
-- The tenant_isolation policy is restated on purpose (the migration RLS policy
-- atomicity gate). It is byte-for-byte the policy 0000 creates, so it changes
-- nothing.
--
-- Idempotent: FORCE is idempotent and the policy is dropped before it is created.
ALTER TABLE public.local_environment FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.local_environment;
CREATE POLICY tenant_isolation ON public.local_environment
  FOR ALL
  USING (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK (tenant_id = current_setting('app.tenant_id', true));

-- The dashboard reader keeps SELECT on this relation: every file restating the
-- policy carries the grant.
GRANT SELECT ON public.local_environment TO app_dashboard;
