# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain guardrails for node_pipeline_audit_orchestrator [OMN-12211]."""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

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


class FakeTicketAdapter:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def create_ticket(self, payload: dict[str, Any]) -> str:
        self.payloads.append(payload)
        return f"OMN-PIPE-{len(self.payloads)}"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _contract() -> dict:  # type: ignore[type-arg]
    path = _repo_root() / "src" / "omnimarket" / "nodes" / _NODE_NAME / "contract.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Contract shape
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pipeline_audit_orchestrator_contract_is_implemented() -> None:
    raw = _contract()

    assert raw["node_not_implemented"] is False
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
# Handler behavior
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pipeline_audit_orchestrator_dry_run_inventories_repos(
    tmp_path: Path,
) -> None:
    _write_repo(
        tmp_path,
        "producer",
        contract_yaml="""
name: node_producer
event_bus:
  publish_topics:
    - onex.evt.sample.created.v1
  subscribe_topics: []
""",
        source="correlation_id = 'abc'\n",
        dockerfile="CMD python -m producer\n",
    )

    result = HandlerPipelineAuditOrchestrator().handle(
        ModelPipelineAuditRequest(
            repos=("producer",),
            audit_type=EnumAuditType.TOPICS,
            dry_run=True,
            omni_home_path=str(tmp_path),
        )
    )

    assert result.run_status == EnumPipelineAuditStatus.DRY_RUN
    assert result.repos_audited == ("producer",)
    assert result.repo_inventories[0].kafka_produce_topics == (
        "onex.evt.sample.created.v1",
    )
    assert result.high_count == 1
    assert result.gap_register[0].proof_category == EnumProofCategory.WIRE_TOPICS
    assert result.tickets_created == ()


@pytest.mark.unit
def test_pipeline_audit_orchestrator_detects_missing_producer_and_fail_fast(
    tmp_path: Path,
) -> None:
    _write_repo(
        tmp_path,
        "consumer",
        contract_yaml="""
name: node_consumer
event_bus:
  publish_topics: []
  subscribe_topics:
    - onex.cmd.sample.create.v1
""",
        source="def handle(event):\n    return event.correlation_id\n",
    )

    result = HandlerPipelineAuditOrchestrator().handle(
        ModelPipelineAuditRequest(
            repos=("consumer",),
            audit_type=EnumAuditType.TOPICS,
            dry_run=True,
            fail_fast=True,
            omni_home_path=str(tmp_path),
        )
    )

    assert result.run_status == EnumPipelineAuditStatus.ABORTED
    assert result.breaking_count == 1
    assert result.gap_register[0].status == EnumFindingStatus.BREAKING


@pytest.mark.unit
def test_pipeline_audit_orchestrator_creates_tickets_through_adapter(
    tmp_path: Path,
) -> None:
    _write_repo(
        tmp_path,
        "producer",
        contract_yaml="""
name: node_producer
event_bus:
  publish_topics:
    - onex.evt.sample.created.v1
  subscribe_topics: []
""",
        source="correlation_id = 'abc'\n",
    )
    adapter = FakeTicketAdapter()

    result = HandlerPipelineAuditOrchestrator(ticket_adapter=adapter).handle(
        ModelPipelineAuditRequest(
            repos=("producer",),
            audit_type=EnumAuditType.TOPICS,
            skip_ticket_creation=False,
            omni_home_path=str(tmp_path),
        )
    )

    assert result.run_status == EnumPipelineAuditStatus.COMPLETED
    assert result.tickets_created == ("OMN-PIPE-1",)
    assert adapter.payloads[0]["labels"] == [
        "pipeline-audit",
        "high",
        "wire_topics",
    ]


@pytest.mark.unit
def test_pipeline_audit_orchestrator_requires_ticket_adapter_for_live_findings(
    tmp_path: Path,
) -> None:
    _write_repo(
        tmp_path,
        "producer",
        contract_yaml="""
name: node_producer
event_bus:
  publish_topics:
    - onex.evt.sample.created.v1
  subscribe_topics: []
""",
    )

    with pytest.raises(RuntimeError, match="ticket adapter required"):
        HandlerPipelineAuditOrchestrator().handle(
            ModelPipelineAuditRequest(
                repos=("producer",),
                audit_type=EnumAuditType.TOPICS,
                omni_home_path=str(tmp_path),
            )
        )


@pytest.mark.unit
def test_pipeline_audit_orchestrator_skip_ticket_creation_is_read_only(
    tmp_path: Path,
) -> None:
    _write_repo(
        tmp_path,
        "producer",
        contract_yaml="""
name: node_producer
event_bus:
  publish_topics:
    - onex.evt.sample.created.v1
  subscribe_topics: []
""",
    )

    result = HandlerPipelineAuditOrchestrator().handle(
        ModelPipelineAuditRequest(
            repos=("producer",),
            audit_type=EnumAuditType.TOPICS,
            skip_ticket_creation=True,
            omni_home_path=str(tmp_path),
        )
    )

    assert result.run_status == EnumPipelineAuditStatus.COMPLETED
    assert result.high_count == 1
    assert result.tickets_created == ()


# ---------------------------------------------------------------------------
# OMN-13693: DLQ topic prefix must be a named constant, not a string literal
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_dlq_topic_prefix_constant_is_importable() -> None:
    """DLQ_TOPIC_PREFIX must exist in the node constants module (OMN-13693)."""
    from omnimarket.nodes.node_pipeline_audit_orchestrator.constants import (
        DLQ_TOPIC_PREFIX,
    )

    assert DLQ_TOPIC_PREFIX == "onex.dlq."


@pytest.mark.unit
def test_dlq_topics_excluded_from_no_consumer_finding(tmp_path: Path) -> None:
    """Topics matching DLQ_TOPIC_PREFIX must not trigger a 'no consumer' finding."""
    _write_repo(
        tmp_path,
        "dlq_producer",
        contract_yaml="""
name: node_dlq_producer
event_bus:
  publish_topics:
    - onex.dlq.sample.failed.v1
  subscribe_topics: []
""",
        source="pass\n",
    )

    result = HandlerPipelineAuditOrchestrator().handle(
        ModelPipelineAuditRequest(
            repos=("dlq_producer",),
            audit_type=EnumAuditType.TOPICS,
            dry_run=True,
            omni_home_path=str(tmp_path),
        )
    )

    # DLQ topics must be suppressed — zero findings expected
    topic_findings = [
        f
        for f in result.gap_register
        if f.proof_category == EnumProofCategory.WIRE_TOPICS
        and "no audited consumer" in (f.description or "")
        and "onex.dlq." in (f.evidence or "")
    ]
    assert topic_findings == [], (
        "DLQ topics must not appear in 'produced but no consumer' findings"
    )


@pytest.mark.unit
def test_handler_source_has_no_literal_dlq_string() -> None:
    """Guard against regression: handler source must not contain the bare string literal."""
    handler_path = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_pipeline_audit_orchestrator"
        / "handlers"
        / "handler_pipeline_audit_orchestrator.py"
    )
    source = handler_path.read_text(encoding="utf-8")
    # The constant name is allowed; the raw literal in a startswith call is not.
    assert '"onex.dlq."' not in source, (
        "handler_pipeline_audit_orchestrator.py must not contain the raw "
        "DLQ topic prefix as a double-quoted string literal — use DLQ_TOPIC_PREFIX from constants"
    )
    assert "'onex.dlq.'" not in source, (
        "handler_pipeline_audit_orchestrator.py must not contain the raw "
        "DLQ topic prefix as a single-quoted string literal — use DLQ_TOPIC_PREFIX from constants"
    )


def _write_repo(
    root: Path,
    name: str,
    *,
    contract_yaml: str,
    source: str = "",
    dockerfile: str = "",
) -> Path:
    repo = root / name
    node_dir = repo / "src" / name / "nodes" / "node_sample"
    node_dir.mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='sample'\n")
    (node_dir / "contract.yaml").write_text(contract_yaml, encoding="utf-8")
    (node_dir / "handler.py").write_text(source, encoding="utf-8")
    if dockerfile:
        (repo / "Dockerfile").write_text(dockerfile, encoding="utf-8")
    return repo
