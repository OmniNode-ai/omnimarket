-- OMN-11243: Create the contract_registry projection table.
-- Stores validated contract snapshots for each omnimarket node.
-- DDL owner: omnimarket.nodes.node_contract_registry

CREATE TABLE IF NOT EXISTS contract_registry (
    id SERIAL PRIMARY KEY,
    node_name TEXT NOT NULL,
    contract_hash TEXT NOT NULL,
    contract_yaml TEXT NOT NULL,
    node_version JSONB NOT NULL DEFAULT '{}',
    status TEXT NOT NULL,
    correlation_id UUID NOT NULL,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    deployer_id TEXT NOT NULL DEFAULT '',
    target_profile TEXT NOT NULL DEFAULT '',
    UNIQUE(node_name, contract_hash)
);

CREATE INDEX IF NOT EXISTS idx_contract_registry_node_name ON contract_registry(node_name);
CREATE INDEX IF NOT EXISTS idx_contract_registry_status ON contract_registry(status);
