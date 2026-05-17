-- OMN-10079: Create the node registration projection table.
-- OMN-11012 ownership: omnimarket.nodes.node_projection_registration is the
-- DDL owner for node_service_registry. Do not add a duplicate CREATE TABLE
-- migration for this table in omnibase_infra.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS node_service_registry (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  service_name TEXT UNIQUE NOT NULL,
  service_url TEXT NOT NULL DEFAULT '',
  service_type TEXT,
  health_status TEXT DEFAULT 'unknown',
  last_health_check TIMESTAMPTZ,
  last_heartbeat_at TIMESTAMPTZ,
  uptime_seconds BIGINT DEFAULT 0,
  health_check_interval_seconds INT DEFAULT 60,
  metadata JSONB DEFAULT '{}'::jsonb,
  is_active BOOLEAN DEFAULT true,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  projected_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_node_service_registry_health_status
  ON node_service_registry (health_status);

CREATE INDEX IF NOT EXISTS idx_node_service_registry_last_heartbeat_at
  ON node_service_registry (last_heartbeat_at);
