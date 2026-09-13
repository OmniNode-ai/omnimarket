# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-17201: register the node-owned hook_events relation in the SQL catalog."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
MANIFEST = REPO_ROOT / "scripts" / "application-relation-ownership.yaml"
CONTRACT = (
    REPO_ROOT
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_hook_event_capture"
    / "contract.yaml"
)
METADATA = CONTRACT.with_name("metadata.yaml")
RELATION = "hook_events"
MIGRATION = (
    "src/omnimarket/nodes/node_hook_event_capture/migrations/"
    "0001_create_hook_events.sql"
)
NODE_OWNER = "node_hook_event_capture"
SERVICE_OWNER = "service:omnimarket_projection_migration_runner"
INVENTORY = REPO_ROOT / "docs" / "evidence" / "OMN-15423-relation-inventory.json"


def _entries(path: Path, section: str) -> list[dict[str, Any]]:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    entries = payload[section]["db_tables"]
    assert isinstance(entries, list)
    return [entry for entry in entries if isinstance(entry, dict)]


@pytest.mark.unit
def test_service_catalog_resolves_the_node_owned_public_relation() -> None:
    """The infra SQL gate sees the same physical table as its owning contract."""
    catalog_matches = [
        entry for entry in _entries(MANIFEST, "db_io") if entry.get("name") == RELATION
    ]
    assert len(catalog_matches) == 1
    catalog = catalog_matches[0]

    contract_matches = [
        entry for entry in _entries(CONTRACT, "db_io") if entry.get("name") == RELATION
    ]
    assert len(contract_matches) == 1
    contract = contract_matches[0]

    for field in ("name", "database_ref", "schema", "access", "role"):
        assert catalog[field] == contract[field]
    assert contract["migration"] == "0001_create_hook_events.sql"
    assert catalog == {
        "name": RELATION,
        "database_ref": "application",
        "schema": "public",
        "migration": MIGRATION,
        "access": "read_write",
        "role": "hook_events",
    }


@pytest.mark.unit
def test_cross_repo_catalog_entry_preserves_node_ddl_ownership() -> None:
    """Registration for the infra SQL gate does not transfer CREATE ownership."""
    metadata = yaml.safe_load(METADATA.read_text(encoding="utf-8"))
    ownership = metadata["ownership"][RELATION]

    assert ownership["ddl_owner"] == "omnimarket.nodes.node_hook_event_capture"
    assert ownership["create_migration"] == "migrations/0001_create_hook_events.sql"
    assert ownership["duplicate_migration_policy"] == "forbid_cross_repo_create_table"
    assert set(ownership["non_owner_repos"]) == {"omnibase_infra", "omninode_infra"}

    service_catalog = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    assert service_catalog["owner_declaration"] == SERVICE_OWNER
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    table_matches = [
        row
        for row in inventory["relations"]
        if row.get("kind") == "table" and row.get("name") == RELATION
    ]
    assert len(table_matches) == 1
    relation = table_matches[0]

    # Negative control: adding the service catalog entry must add an accessor,
    # while the generated physical DDL owner remains the node that created it.
    assert SERVICE_OWNER in relation["accessor_nodes"]
    assert relation["owner_declaration"] == NODE_OWNER
    assert relation["owner_declaration"] != service_catalog["owner_declaration"]
    assert relation["producer"] == NODE_OWNER
    assert MIGRATION in relation["authoritative_sources"]
    assert "scripts/application-relation-ownership.yaml" in relation["contract_sources"]
