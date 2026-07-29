-- OMN-14974: reconcile a staging database whose node migration ledger drifted
-- ahead of the materialized node_service_registry shape. This net-new migration
-- is intentionally idempotent so the runner applies it even when historical
-- 0001 is recorded while its columns are absent.

-- The registration owner migrations are operator-fenced while the RLS ruling is
-- unresolved. A fresh database therefore does not have the base table. Warm
-- databases do, and are the only databases this reconciliation is meant to
-- repair. Keep the migration deterministic on both paths.
DO $reconcile$
BEGIN
  IF to_regclass('public.node_service_registry') IS NOT NULL THEN
    ALTER TABLE node_service_registry
      ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ,
      ADD COLUMN IF NOT EXISTS uptime_seconds BIGINT NOT NULL DEFAULT 0;

    UPDATE node_service_registry
    SET last_heartbeat_at = last_health_check
    WHERE last_heartbeat_at IS NULL
      AND last_health_check IS NOT NULL;

    CREATE INDEX IF NOT EXISTS idx_node_service_registry_last_heartbeat_at
      ON node_service_registry (last_heartbeat_at);
  ELSE
    RAISE NOTICE 'node_service_registry is absent; heartbeat reconciliation is a no-op';
  END IF;
END
$reconcile$;
