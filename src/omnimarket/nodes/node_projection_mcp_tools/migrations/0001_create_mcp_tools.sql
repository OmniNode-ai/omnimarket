-- OMN-13080: Create the mcp_tools snapshot projection table.
-- DDL owner: omnimarket.nodes.node_projection_mcp_tools.
-- Consumed by: omnidash mcp-tools widget via onex.snapshot.projection.mcp-tools.v1.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS mcp_tools (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  tool_name TEXT UNIQUE NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  model_id TEXT NOT NULL DEFAULT '',
  correlation_id TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  is_active BOOLEAN NOT NULL DEFAULT true,
  mcp_tags TEXT[] NOT NULL DEFAULT '{}',
  metadata JSONB NOT NULL DEFAULT '{}',
  registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  projected_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mcp_tools_status
  ON mcp_tools (status);

CREATE INDEX IF NOT EXISTS idx_mcp_tools_registered_at
  ON mcp_tools (registered_at DESC);

CREATE INDEX IF NOT EXISTS idx_mcp_tools_is_active
  ON mcp_tools (is_active);
