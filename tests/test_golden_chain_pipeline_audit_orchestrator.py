# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain guardrails for node_pipeline_audit_orchestrator [OMN-12211].

Honest routing behaviour for an explicit stub node:
- contract marks node_not_implemented: true
- entry point loads
- typed models are strict (frozen, extra="forbid")
- handler fails loudly with NotImplementedError containing "node_not_implemented"
- contract declares the expected runtime routing surface
"""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_pipeline_audit_orchestrator.handlers.handler_pipeline_audit_orchestrator import (
    HandlerPipelineAuditOrchestrator,
)
from omnimarket.nodes.node_pipeline_audit_orchestrator.models.model_pipeline_audit_request import (
    EnumAuditType,
    EnumPipelineSize,
    ModelPipelineAuditRequest,
)
from omnimarket.nodes.node_pipeline_audit_orchestrator.models.model_pipeline_audit_result import (
    EnumFindingSeverity,
    EnumFindingStatus,
    EnumPipelineAuditStatus,
    EnumProofCategory,
    ModelGapFinding,
    ModelPipelineAuditResult,
    ModelRepoInventory,
)

_NODE_NAME = "node_pipeline_audit_orchestrator"
_HANDLER_MODULE = "omnimarket.nodes.node_pipeline_audit_orchestrator.handlers.handler_pipeline_audit_orchestrator"
_HANDLER_CLASS = "HandlerPipelineAuditOrchestrator"
_REQUEST_MODULE = "omnimarket.nodes.node_pipeline_audit_orchestrator.models.model_pipeline_audit_request"
_REQUEST_CLASS = "ModelPipelineAuditRequest"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _contract() -> dict:  # type: ignore[type-arg]
    path = _repo_root() / "src" / "omnimarket" / "nodes" / _NODE_NAME / "contract.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Contract shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pipeline_audit_orchestrator_contract_is_explicit_stub() -> None:
    raw = _contract()

    assert raw["node_not_implemented"] is True
    assert raw["node_type"] == "orchestrator"
    assert raw["handler"]["module"] == _HANDLER_MODULE
    assert raw["handler"]["class"] == _HANDLER_CLASS
    assert raw["handler"]["input_model"] == f"{_REQUEST_MODULE}.{_REQUEST_CLASS}"


@pytest.mark.unit
def test_pipeline_audit_orchestrator_contract_routing_surface() -> None:
    raw = _contract()

    assert raw["handler_routing"]["routing_strategy"] == "operation_match"
    assert raw["handler_routing"]["handlers"] == [
        {
            "handler": {
                "name": _HANDLER_CLASS,
                "module": _HANDLER_MODULE,
            }
        }
    ]


@pytest.mark.unit
def test_pipeline_audit_orchestrator_contract_event_bus() -> None:
    raw = _contract()
    eb = raw["event_bus"]

    assert eb["consumer_group"] == "omnimarket.pipeline_audit_orchestrator.consume.v1"
    assert "onex.cmd.omnimarket.pipeline-audit-start.v1" in eb["subscribe_topics"]
    assert "onex.evt.omnimarket.pipeline-audit-completed.v1" in eb["publish_topics"]
    assert (
        "onex.evt.omnimarket.pipeline-audit-repo-inventoried.v1" in eb["publish_topics"]
    )
    assert "onex.evt.omnimarket.pipeline-audit-gap-found.v1" in eb["publish_topics"]
    assert "onex.dlq.omnimarket.pipeline-audit.v1" in eb["dlq_topics"]


@pytest.mark.unit
def test_pipeline_audit_orchestrator_terminal_event() -> None:
    raw = _contract()
    assert raw["terminal_event"] == "onex.evt.omnimarket.pipeline-audit-completed.v1"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pipeline_audit_orchestrator_entry_point_loads() -> None:
    eps = {ep.name: ep for ep in entry_points(group="onex.nodes")}

    loaded = eps[_NODE_NAME].load()

    assert loaded.__name__ == f"omnimarket.nodes.{_NODE_NAME}"


# ---------------------------------------------------------------------------
# Input model (ModelPipelineAuditRequest)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_model_pipeline_audit_request_minimal() -> None:
    req = ModelPipelineAuditRequest(repos=("omniclaude", "omnibase_core"))

    assert req.repos == ("omniclaude", "omnibase_core")
    assert req.audit_type == EnumAuditType.FULL
    assert req.parallel is True
    assert req.pipeline_size == EnumPipelineSize.MEDIUM
    assert req.dry_run is False
    assert req.skip_ticket_creation is False
    assert req.fail_fast is False
    assert req.omni_home_path == ""


@pytest.mark.unit
def test_model_pipeline_audit_request_all_flags() -> None:
    req = ModelPipelineAuditRequest(
        repos=("omniclaude", "omnibase_core", "omnibase_infra"),
        audit_type=EnumAuditType.TOPICS,
        parallel=False,
        pipeline_size=EnumPipelineSize.LARGE,
        dry_run=True,
        skip_ticket_creation=True,
        fail_fast=True,
        omni_home_path="/opt/omni_home",
    )

    assert req.audit_type == EnumAuditType.TOPICS
    assert req.parallel is False
    assert req.pipeline_size == EnumPipelineSize.LARGE
    assert req.dry_run is True
    assert req.skip_ticket_creation is True
    assert req.fail_fast is True


@pytest.mark.unit
def test_model_pipeline_audit_request_empty_repos() -> None:
    req = ModelPipelineAuditRequest(repos=())

    assert req.repos == ()


@pytest.mark.unit
def test_model_pipeline_audit_request_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ModelPipelineAuditRequest(
            repos=("omniclaude",),
            unexpected_field=True,  # type: ignore[call-arg]
        )


@pytest.mark.unit
def test_model_pipeline_audit_request_is_frozen() -> None:
    req = ModelPipelineAuditRequest(repos=("omniclaude",))

    with pytest.raises(ValidationError):
        req.parallel = False  # type: ignore[misc]


@pytest.mark.unit
def test_enum_audit_type_values() -> None:
    assert EnumAuditType.FULL == "full"
    assert EnumAuditType.TOPICS == "topics"
    assert EnumAuditType.SCHEMA == "schema"
    assert EnumAuditType.ENTRYPOINT == "entrypoint"
    assert EnumAuditType.WIRE_FORMAT == "wire_format"
    assert EnumAuditType.CORRELATION == "correlation"


@pytest.mark.unit
def test_enum_pipeline_size_values() -> None:
    assert EnumPipelineSize.SMALL == "small"
    assert EnumPipelineSize.MEDIUM == "medium"
    assert EnumPipelineSize.LARGE == "large"


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_model_pipeline_audit_result_minimal() -> None:
    result = ModelPipelineAuditResult(
        run_status=EnumPipelineAuditStatus.COMPLETED,
    )

    assert result.run_status == EnumPipelineAuditStatus.COMPLETED
    assert result.repos_audited == ()
    assert result.repo_inventories == ()
    assert result.gap_register == ()
    assert result.breaking_count == 0
    assert result.critical_count == 0
    assert result.high_count == 0
    assert result.medium_count == 0
    assert result.low_count == 0
    assert result.tickets_created == ()
    assert result.dry_run is False


@pytest.mark.unit
def test_model_pipeline_audit_result_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ModelPipelineAuditResult(
            run_status=EnumPipelineAuditStatus.COMPLETED,
            bogus_field=True,  # type: ignore[call-arg]
        )


@pytest.mark.unit
def test_model_repo_inventory_minimal() -> None:
    inv = ModelRepoInventory(
        repo="omniclaude",
        repo_path="/opt/omni_home/omniclaude",
    )

    assert inv.repo == "omniclaude"
    assert inv.kafka_produce_topics == ()
    assert inv.kafka_consume_topics == ()
    assert inv.db_tables_write == ()
    assert inv.db_tables_read == ()
    assert inv.entrypoint_status == ""
    assert inv.inventory_json == ""


@pytest.mark.unit
def test_model_repo_inventory_full() -> None:
    inv = ModelRepoInventory(
        repo="omnibase_infra",
        repo_path="/opt/omni_home/omnibase_infra",
        pipeline_role="Event consumer and database writer",
        kafka_produce_topics=("onex.evt.omnibase.session-stored.v1",),
        kafka_consume_topics=("onex.cmd.omnibase.session-store.v1",),
        db_tables_write=("sessions",),
        db_tables_read=(),
        entrypoint_command="python -m omnibase_infra.main",
        entrypoint_status="REAL",
        inventory_json='{"kafka": {}}',
    )

    assert inv.pipeline_role == "Event consumer and database writer"
    assert len(inv.kafka_produce_topics) == 1
    assert inv.entrypoint_status == "REAL"


@pytest.mark.unit
def test_model_repo_inventory_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ModelRepoInventory(
            repo="omniclaude",
            repo_path="/tmp",
            bogus=True,  # type: ignore[call-arg]
        )


@pytest.mark.unit
def test_model_gap_finding_fields() -> None:
    finding = ModelGapFinding(
        finding_id=1,
        severity=EnumFindingSeverity.BREAKING,
        proof_category=EnumProofCategory.WIRE_TOPICS,
        description="Producer topic 'onex.cmd.omnibase.store.v1' has no matching consumer",
        producer_repo="omnibase_core",
        consumer_repo="omnibase_infra",
        evidence_location="src/omnibase_core/nodes/handler.py:42",
        proposed_fix="Add subscribe for 'onex.cmd.omnibase.store.v1' in omnibase_infra consumer",
        status=EnumFindingStatus.GAP,
    )

    assert finding.finding_id == 1
    assert finding.severity == EnumFindingSeverity.BREAKING
    assert finding.proof_category == EnumProofCategory.WIRE_TOPICS
    assert finding.status == EnumFindingStatus.GAP


@pytest.mark.unit
def test_model_gap_finding_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ModelGapFinding(
            finding_id=1,
            severity=EnumFindingSeverity.LOW,
            proof_category=EnumProofCategory.CORRELATION,
            description="Correlation ID break",
            extra_nonsense=True,  # type: ignore[call-arg]
        )


@pytest.mark.unit
def test_enum_finding_severity_values() -> None:
    assert EnumFindingSeverity.BREAKING == "breaking"
    assert EnumFindingSeverity.CRITICAL == "critical"
    assert EnumFindingSeverity.HIGH == "high"
    assert EnumFindingSeverity.MEDIUM == "medium"
    assert EnumFindingSeverity.LOW == "low"


@pytest.mark.unit
def test_enum_proof_category_values() -> None:
    assert EnumProofCategory.ENTRYPOINT == "entrypoint"
    assert EnumProofCategory.DSN == "dsn"
    assert EnumProofCategory.WIRE_TOPICS == "wire_topics"
    assert EnumProofCategory.SCHEMA_HANDSHAKE == "schema_handshake"
    assert EnumProofCategory.WIRE_FORMAT == "wire_format"
    assert EnumProofCategory.CORRELATION == "correlation"


@pytest.mark.unit
def test_enum_pipeline_audit_status_values() -> None:
    assert EnumPipelineAuditStatus.COMPLETED == "completed"
    assert EnumPipelineAuditStatus.PARTIAL == "partial"
    assert EnumPipelineAuditStatus.FAILED == "failed"
    assert EnumPipelineAuditStatus.DRY_RUN == "dry_run"
    assert EnumPipelineAuditStatus.ABORTED == "aborted"


@pytest.mark.unit
def test_enum_finding_status_values() -> None:
    assert EnumFindingStatus.PROVEN == "proven"
    assert EnumFindingStatus.GAP == "gap"
    assert EnumFindingStatus.MISMATCH == "mismatch"
    assert EnumFindingStatus.BREAKING == "breaking"


# ---------------------------------------------------------------------------
# Handler stub (fails loudly)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pipeline_audit_orchestrator_handler_fails_loudly() -> None:
    handler = HandlerPipelineAuditOrchestrator()
    request = ModelPipelineAuditRequest(repos=("omniclaude", "omnibase_core"))

    with pytest.raises(NotImplementedError, match="node_not_implemented"):
        handler.handle(request)
