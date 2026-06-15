"""Static projection materialization ratchet tests for OMN-12980."""

from __future__ import annotations

import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from omnimarket.projection.discovery import discover_contracts
from omnimarket.projection.validation import (
    ProjectionMaterializationValidationError,
    assert_projection_materialization_contracts_ready,
    validate_projection_materialization_contracts,
)


@dataclass(frozen=True)
class _ContractStub:
    name: str
    contract_path: Path


@dataclass(frozen=True)
class _ManifestStub:
    contracts: tuple[_ContractStub, ...]


def _write_node(
    tmp_path: Path,
    *,
    node_dir_name: str,
    contract_yaml: str,
    metadata_yaml: str | None = None,
    migrations: dict[str, str] | None = None,
) -> _ContractStub:
    node_dir = tmp_path / node_dir_name
    node_dir.mkdir()
    contract_path = node_dir / "contract.yaml"
    contract_path.write_text(textwrap.dedent(contract_yaml))

    if metadata_yaml is not None:
        (node_dir / "metadata.yaml").write_text(textwrap.dedent(metadata_yaml))

    if migrations:
        migrations_dir = node_dir / "migrations"
        migrations_dir.mkdir()
        for filename, sql in migrations.items():
            (migrations_dir / filename).write_text(textwrap.dedent(sql))

    return _ContractStub(name=node_dir_name, contract_path=contract_path)


def test_exposed_projection_without_authority_fails_validation(
    tmp_path: Path,
) -> None:
    contract = _write_node(
        tmp_path,
        node_dir_name="node_missing_authority",
        contract_yaml="""
        name: node_missing_authority
        projection_api:
          expose: true
          topic: onex.snapshot.projection.missing-authority.v1
          table: missing_authority_projection
          schema: public
          columns: [id, updated_at]
        """,
    )

    issues = validate_projection_materialization_contracts(_ManifestStub((contract,)))

    assert len(issues) == 1
    issue = issues[0]
    assert issue.code == "missing_materialization_authority"
    assert issue.source_contract == "node_missing_authority"
    assert issue.topic == "onex.snapshot.projection.missing-authority.v1"
    assert issue.table_or_view == "missing_authority_projection"
    assert "missing materialization authority" in issue.detail


def test_exposed_projection_without_cold_ddl_proof_fails_validation(
    tmp_path: Path,
) -> None:
    contract = _write_node(
        tmp_path,
        node_dir_name="node_missing_cold_proof",
        contract_yaml="""
        name: node_missing_cold_proof
        db_io:
          db_tables:
            - name: missing_cold_projection
              migration: "0001_create_projection.sql"
              access: write
              database: omnidash_analytics
              role: projection_read_model
        projection_api:
          expose: true
          topic: onex.snapshot.projection.missing-cold-proof.v1
          table: missing_cold_projection
          schema: public
          columns: [id, updated_at]
        """,
        migrations={
            "0001_create_projection.sql": """
            CREATE TABLE IF NOT EXISTS different_projection (
                id TEXT PRIMARY KEY
            );
            """,
        },
    )

    issues = validate_projection_materialization_contracts(_ManifestStub((contract,)))

    assert len(issues) == 1
    issue = issues[0]
    assert issue.code == "missing_cold_table_proof"
    assert issue.source_contract == "node_missing_cold_proof"
    assert issue.topic == "onex.snapshot.projection.missing-cold-proof.v1"
    assert issue.table_or_view == "missing_cold_projection"
    assert "db_io.db_tables[missing_cold_projection].migration" in issue.detail
    assert "no node-local migration creates this table/view" in issue.detail


def test_projection_materialization_error_names_contract_topic_table_and_authority(
    tmp_path: Path,
) -> None:
    contract = _write_node(
        tmp_path,
        node_dir_name="node_error_message",
        contract_yaml="""
        name: node_error_message
        projection_api:
          expose: true
          topic: onex.snapshot.projection.error-message.v1
          table: error_message_projection
          schema: public
          columns: [id]
        """,
    )

    with pytest.raises(ProjectionMaterializationValidationError) as exc_info:
        assert_projection_materialization_contracts_ready(_ManifestStub((contract,)))

    message = str(exc_info.value)
    assert "contract=node_error_message" in message
    assert "topic=onex.snapshot.projection.error-message.v1" in message
    assert "table/view=public.error_message_projection" in message
    assert "missing materialization authority" in message


def test_exposed_projection_with_node_local_view_migration_passes(
    tmp_path: Path,
) -> None:
    contract = _write_node(
        tmp_path,
        node_dir_name="node_view_projection",
        contract_yaml="""
        name: node_view_projection
        projection_api:
          expose: true
          topic: onex.snapshot.projection.view.v1
          table: view_projection
          schema: public
          columns: [id, updated_at]
        """,
        migrations={
            "0001_create_projection_view.sql": """
            CREATE OR REPLACE VIEW view_projection AS
            SELECT 'id-1'::text AS id, now() AS updated_at;
            """,
        },
    )

    assert (
        validate_projection_materialization_contracts(_ManifestStub((contract,))) == ()
    )


def test_current_materialized_projection_contracts_pass_scoped_ratchet() -> None:
    # TODO(OMN-12942/OMN-12970 and peer instance-fix tickets): expand this
    # scope to every exposed projection contract after the remaining DDL-owner
    # repairs land. OMN-12980 owns the general validator, not those instances.
    validated_contracts = {
        "node_deployment_evidence_reducer",
        "node_evidence_dashboard_reducer",
        "projection_delegation",
        "projection_llm_routing",
        "projection_overnight",
        "projection_registration",
        "projection_savings",
    }
    manifest = discover_contracts()

    assert_projection_materialization_contracts_ready(
        manifest,
        contract_names=validated_contracts,
    )
