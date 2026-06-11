# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Regression tests for OMN-12936.

Two defects classified BROKEN in the 2026-06-11 full-feature pass:

1. The evidence/readiness ``coerce(**payload)`` family splatted the runtime
   transport envelope (``{"payload": {...}, "partition_key": ...}``) straight
   into domain-model construction, raising a ``ValidationError`` with every
   required field reported missing. The readiness gate orchestrator shared this
   defect class with the evidence pipeline.
2. ``projection_overnight_readiness`` (a view over ``overnight_sessions``) had
   no node-owned migration to create its base tables, so the projection-api
   served HTTP 503 ``table 'public.projection_overnight_readiness' not found at
   startup``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_compat.contracts.evidence_pipeline.wire.model_deployment_readiness_result import (
    ModelDeploymentReadinessResult,
)
from omnibase_compat.contracts.evidence_pipeline.wire.model_evidence_pipeline_command import (
    ModelEvidencePipelineCommand,
)
from omnibase_compat.contracts.evidence_pipeline.wire.model_gap_report import (
    ModelGapReport,
)

from omnimarket.nodes.evidence_pipeline_native import (
    coerce_command,
    coerce_gap,
    coerce_readiness,
)
from omnimarket.nodes.node_readiness_gate_orchestrator import (
    HandlerReadinessGateOrchestrator,
)

NODE_MIGRATIONS = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_overnight/migrations"
)


def _gap_report() -> ModelGapReport:
    return ModelGapReport(
        correlation_id="cid-omn-12936",
        validation_run_id="run-omn-12936",
        deployment_id="deploy-omn-12936",
        generated_at="2026-06-11T02:00:00Z",
        validator_version="evidence-readiness-native-v1",
        gap_classifications={},
        validation_result_refs=("sha256:ref-1",),
    )


def _readiness_result() -> ModelDeploymentReadinessResult:
    return ModelDeploymentReadinessResult(
        correlation_id="cid-omn-12936",
        validation_run_id="run-omn-12936",
        deployment_id="deploy-omn-12936",
        readiness_state="READY",
        scored_at="2026-06-11T02:00:00Z",
        validator_version="evidence-readiness-native-v1",
        gap_report_hash="sha256:gap-hash",
    )


def _command() -> ModelEvidencePipelineCommand:
    return ModelEvidencePipelineCommand(
        correlation_id="cid-omn-12936",
        validation_run_id="run-omn-12936",
        ticket_id="OMN-12936",
        repository="omnimarket",
        source_commit_sha="abcdef1234567890",
        requested_at="2026-06-11T02:00:00Z",
        trigger_surface="manual",
    )


def _enveloped(model: object) -> dict[str, object]:
    """Mimic the dispatch-engine materialized envelope around a domain payload."""
    return {
        "payload": model.model_dump(mode="json"),  # type: ignore[attr-defined]
        "partition_key": None,
        "event_type": "onex.cmd.omnimarket.readiness-gate-start.v1",
    }


def test_coerce_gap_unwraps_transport_envelope() -> None:
    gap = _gap_report()
    coerced = coerce_gap(_enveloped(gap))
    assert coerced == gap


def test_coerce_readiness_unwraps_transport_envelope() -> None:
    readiness = _readiness_result()
    coerced = coerce_readiness(_enveloped(readiness))
    assert coerced == readiness


def test_coerce_command_unwraps_transport_envelope() -> None:
    command = _command()
    coerced = coerce_command(_enveloped(command))
    assert coerced == command


def test_coerce_gap_still_accepts_bare_payload() -> None:
    gap = _gap_report()
    assert coerce_gap(gap.model_dump(mode="json")) == gap
    assert coerce_gap(gap) is gap


def test_readiness_gate_handles_enveloped_gap_report() -> None:
    gap = _gap_report()
    result = HandlerReadinessGateOrchestrator().handle(_enveloped(gap))
    assert isinstance(result, ModelDeploymentReadinessResult)
    assert result.readiness_state == "READY"
    assert result.correlation_id == gap.correlation_id


def test_readiness_gate_routes_enveloped_readiness_result_directly() -> None:
    readiness = _readiness_result()
    result = HandlerReadinessGateOrchestrator().handle(_enveloped(readiness))
    assert result.readiness_state == "READY"
    assert result.gap_report_hash == readiness.gap_report_hash


def test_readiness_gate_still_handles_bare_gap_report() -> None:
    gap = _gap_report()
    result = HandlerReadinessGateOrchestrator().handle(gap)
    assert result.correlation_id == gap.correlation_id


def test_overnight_sessions_base_migration_exists_before_view() -> None:
    base = NODE_MIGRATIONS / "0000_create_overnight_sessions_tables.sql"
    view = NODE_MIGRATIONS / "0001_create_overnight_readiness_projection_view.sql"
    assert base.exists(), "base-table migration must exist for the view"
    # Filename order is the migration-runner apply order: base before view.
    assert base.name < view.name
    base_sql = base.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS overnight_sessions" in base_sql
    assert "CREATE TABLE IF NOT EXISTS overnight_session_phases" in base_sql


def test_view_migration_depends_on_base_table() -> None:
    view = NODE_MIGRATIONS / "0001_create_overnight_readiness_projection_view.sql"
    view_sql = view.read_text(encoding="utf-8")
    assert "CREATE OR REPLACE VIEW projection_overnight_readiness" in view_sql
    assert "FROM overnight_sessions" in view_sql


@pytest.mark.parametrize("migration_name", ["0000", "0001"])
def test_node_migrations_are_idempotent(migration_name: str) -> None:
    matches = list(NODE_MIGRATIONS.glob(f"{migration_name}_*.sql"))
    assert len(matches) == 1
    sql = matches[0].read_text(encoding="utf-8")
    # Re-application over an infra-seeded DB must be a no-op.
    assert "IF NOT EXISTS" in sql or "CREATE OR REPLACE" in sql
