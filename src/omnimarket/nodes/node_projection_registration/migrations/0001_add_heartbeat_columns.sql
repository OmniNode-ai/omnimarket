-- OMN-8693: Add last_heartbeat_at and uptime_seconds columns to node_service_registry.
--
-- last_heartbeat_at: updated on every periodic heartbeat event (every 60s per node)
-- uptime_seconds:    node uptime reported by the heartbeat emitter
-- health_status now supports 'stale' in addition to healthy/degraded/unhealthy/unknown

ALTER TABLE node_service_registry
  ADD COLUMN IF NOT EXISTS last_heartbeat_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS uptime_seconds BIGINT NOT NULL DEFAULT 0;

-- Backfill: treat existing rows' last_health_check as the initial heartbeat time
UPDATE node_service_registry
SET last_heartbeat_at = last_health_check
WHERE last_heartbeat_at IS NULL
  AND last_health_check IS NOT NULL;

-- Create index for stale-detection queries (WHERE last_heartbeat_at < NOW() - INTERVAL '5 min')
CREATE INDEX IF NOT EXISTS idx_node_service_registry_last_heartbeat_at
  ON node_service_registry (last_heartbeat_at);
