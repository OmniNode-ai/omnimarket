# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain guardrails for node_decision_store_orchestrator [OMN-12219].

This node is intentionally not implemented yet. The golden chain verifies:
- contract marks the node as not implemented
- typed models are strict (extra="forbid")
- enums are string-based with correct members
- entry point loads
- handler fails loudly with NotImplementedError
"""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

NODE_NAME = "node_decision_store_orchestrator"
HANDLER_MODULE = (
    "omnimarket.nodes.node_decision_store_orchestrator"
    ".handlers.handler_decision_store_orchestrator"
)
HANDLER_CLASS = "HandlerDecisionStoreOrchestrator"
REQUEST_MODULE = (
    "omnimarket.nodes.node_decision_store_orchestrator"
    ".models.model_decision_store_request"
)
REQUEST_CLASS = "ModelDecisionStoreRequest"
RESULT_CLASS = "ModelDecisionStoreResult"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _contract_path() -> Path:
    return _repo_root() / "src" / "omnimarket" / "nodes" / NODE_NAME / "contract.yaml"


@pytest.mark.unit
def test_contract_marks_node_not_implemented() -> None:
    raw = yaml.safe_load(_contract_path().read_text(encoding="utf-8"))

    assert raw["node_not_implemented"] is True
    assert raw["node_type"] == "orchestrator"
    assert raw["handler"]["module"] == HANDLER_MODULE
    assert raw["handler"]["class"] == HANDLER_CLASS
    assert raw["handler"]["input_model"] == f"{REQUEST_MODULE}.{REQUEST_CLASS}"


@pytest.mark.unit
def test_contract_declares_event_bus_surfaces() -> None:
    raw = yaml.safe_load(_contract_path().read_text(encoding="utf-8"))

    assert raw["handler_routing"]["routing_strategy"] == "operation_match"
    assert raw["handler_routing"]["handlers"] == [
        {
            "handler": {
                "name": HANDLER_CLASS,
                "module": HANDLER_MODULE,
            }
        }
    ]
    eb = raw["event_bus"]
    assert eb["consumer_group"] == "omnimarket.decision_store_orchestrator.consume.v1"
    assert "onex.cmd.omnimarket.decision-store-start.v1" in eb["subscribe_topics"]
    assert "onex.evt.omnimarket.decision-stored.v1" in eb["publish_topics"]
    assert "onex.evt.omnimarket.decision-conflict-detected.v1" in eb["publish_topics"]
    assert "onex.dlq.omnimarket.decision-store.v1" in eb["dlq_topics"]


@pytest.mark.unit
def test_entry_point_loads() -> None:
    eps = {ep.name: ep for ep in entry_points(group="onex.nodes")}

    loaded = eps[NODE_NAME].load()

    assert loaded.__name__ == f"omnimarket.nodes.{NODE_NAME}"


@pytest.mark.unit
def test_enum_decision_action_members() -> None:
    mod = import_module(REQUEST_MODULE)
    EnumDecisionAction = mod.EnumDecisionAction  # noqa: N806

    assert EnumDecisionAction.RECORD.value == "record"
    assert EnumDecisionAction.QUERY.value == "query"
    assert EnumDecisionAction.CHECK_CONFLICTS.value == "check_conflicts"


@pytest.mark.unit
def test_enum_decision_type_members() -> None:
    mod = import_module(REQUEST_MODULE)
    EnumDecisionType = mod.EnumDecisionType  # noqa: N806

    expected = {
        "TECH_STACK_CHOICE",
        "DESIGN_PATTERN",
        "API_CONTRACT",
        "SCOPE_BOUNDARY",
        "REQUIREMENT_CHOICE",
    }
    assert {m.name for m in EnumDecisionType} == expected


@pytest.mark.unit
def test_enum_conflict_severity_members() -> None:
    mod = import_module(REQUEST_MODULE)
    EnumConflictSeverity = mod.EnumConflictSeverity  # noqa: N806

    assert {m.name for m in EnumConflictSeverity} == {"LOW", "MEDIUM", "HIGH"}


@pytest.mark.unit
def test_request_model_record_action_is_strict() -> None:
    mod = import_module(REQUEST_MODULE)
    ModelDecisionStoreRequest = getattr(mod, REQUEST_CLASS)  # noqa: N806
    ModelDecisionEntry = mod.ModelDecisionEntry  # noqa: N806
    EnumDecisionAction = mod.EnumDecisionAction  # noqa: N806
    EnumDecisionType = mod.EnumDecisionType  # noqa: N806
    EnumDecisionLayer = mod.EnumDecisionLayer  # noqa: N806

    entry = ModelDecisionEntry(
        decision_type=EnumDecisionType.TECH_STACK_CHOICE,
        domain="routing",
        layer=EnumDecisionLayer.ARCHITECTURE,
        summary="Use Kafka for all inter-service transport",
        rationale="Kafka provides replay, ordering, and backpressure guarantees.",
    )
    req = ModelDecisionStoreRequest(
        action=EnumDecisionAction.RECORD,
        entry=entry,
        dry_run=False,
    )
    assert req.action == EnumDecisionAction.RECORD
    assert req.entry is not None
    assert req.entry.domain == "routing"
    assert req.dry_run is False

    with pytest.raises(ValidationError):
        ModelDecisionStoreRequest(
            action=EnumDecisionAction.RECORD,
            entry=entry,
            unexpected_field=True,
        )


@pytest.mark.unit
def test_request_model_query_action_is_strict() -> None:
    mod = import_module(REQUEST_MODULE)
    ModelDecisionStoreRequest = getattr(mod, REQUEST_CLASS)  # noqa: N806
    ModelDecisionQueryFilter = mod.ModelDecisionQueryFilter  # noqa: N806
    EnumDecisionAction = mod.EnumDecisionAction  # noqa: N806

    qf = ModelDecisionQueryFilter(domain="routing", limit=10)
    req = ModelDecisionStoreRequest(
        action=EnumDecisionAction.QUERY,
        query_filter=qf,
        dry_run=False,
    )
    assert req.action == EnumDecisionAction.QUERY
    assert req.query_filter is not None
    assert req.query_filter.limit == 10

    with pytest.raises(ValidationError):
        ModelDecisionQueryFilter(domain="routing", unexpected_field=True)


@pytest.mark.unit
def test_result_model_is_strict() -> None:
    mod = import_module(REQUEST_MODULE)
    ModelDecisionStoreResult = getattr(mod, RESULT_CLASS)  # noqa: N806
    EnumDecisionAction = mod.EnumDecisionAction  # noqa: N806

    result = ModelDecisionStoreResult(
        action=EnumDecisionAction.RECORD,
        stored_decision_id="abc-123",
        dry_run=False,
    )
    assert result.stored_decision_id == "abc-123"
    assert result.conflicts_found == ()
    assert result.high_severity_count == 0
    assert result.slack_gate_triggered is False
    assert result.query_results == ()
    assert result.next_cursor is None

    with pytest.raises(ValidationError):
        ModelDecisionStoreResult(
            action=EnumDecisionAction.RECORD,
            dry_run=False,
            unexpected_field=True,
        )


@pytest.mark.unit
def test_handler_raises_not_implemented() -> None:
    mod = import_module(HANDLER_MODULE)
    HandlerDecisionStoreOrchestrator = getattr(mod, HANDLER_CLASS)  # noqa: N806

    req_mod = import_module(REQUEST_MODULE)
    ModelDecisionStoreRequest = getattr(req_mod, REQUEST_CLASS)  # noqa: N806
    ModelDecisionEntry = req_mod.ModelDecisionEntry  # noqa: N806
    EnumDecisionAction = req_mod.EnumDecisionAction  # noqa: N806
    EnumDecisionType = req_mod.EnumDecisionType  # noqa: N806
    EnumDecisionLayer = req_mod.EnumDecisionLayer  # noqa: N806

    entry = ModelDecisionEntry(
        decision_type=EnumDecisionType.API_CONTRACT,
        domain="api",
        layer=EnumDecisionLayer.DESIGN,
        summary="All APIs use Pydantic models for request/response",
        rationale="Consistency across services.",
    )
    request = ModelDecisionStoreRequest(
        action=EnumDecisionAction.RECORD,
        entry=entry,
        dry_run=False,
    )

    handler = HandlerDecisionStoreOrchestrator()
    with pytest.raises(NotImplementedError, match="OMN-12219"):
        handler.handle(request)
