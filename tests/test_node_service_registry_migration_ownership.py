from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
NODE_DIR = ROOT / "src/omnimarket/nodes/node_projection_registration"
METADATA = NODE_DIR / "metadata.yaml"
CREATE_MIGRATION = NODE_DIR / "migrations/0000_create_node_service_registry.sql"
OWNERSHIP_DOC = ROOT / "docs/migrations/node-service-registry-ownership.md"


def test_metadata_declares_node_service_registry_ddl_owner() -> None:
    metadata = yaml.safe_load(METADATA.read_text())

    ownership = metadata["ownership"]["node_service_registry"]
    assert ownership == {
        "ddl_owner": "omnimarket.nodes.node_projection_registration",
        "table_role": "projection_read_model",
        "create_migration": "migrations/0000_create_node_service_registry.sql",
        "duplicate_migration_policy": "forbid_cross_repo_create_table",
        "non_owner_repos": ["omnibase_infra"],
        "rationale": (
            "node_service_registry is the omnimarket projection/API read model "
            "fed by this node. omnibase_infra owns runtime registration storage "
            "and registration_projections, not this table's CREATE TABLE "
            "migration.\n"
        ),
    }


def test_metadata_points_at_create_migration_for_declared_table() -> None:
    metadata = yaml.safe_load(METADATA.read_text())

    migration = metadata["ownership"]["node_service_registry"]["create_migration"]
    assert NODE_DIR / migration == CREATE_MIGRATION
    assert (NODE_DIR / migration).is_file()


def test_create_migration_carries_cross_repo_ownership_fence() -> None:
    content = CREATE_MIGRATION.read_text()

    assert "OMN-11012 ownership" in content
    assert "omnimarket.nodes.node_projection_registration is the" in content
    assert "Do not add a duplicate CREATE TABLE" in content
    assert "omnibase_infra" in content


def test_ownership_doc_records_infra_non_owner_status() -> None:
    content = OWNERSHIP_DOC.read_text()

    assert "`omnimarket.nodes.node_projection_registration` is the DDL owner" in content
    assert "`omnibase_infra` owns runtime registration storage" in content
