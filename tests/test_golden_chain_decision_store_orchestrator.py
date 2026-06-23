# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain guardrails for node_decision_store_orchestrator [OMN-12219]."""

from __future__ import annotations

from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_decision_store_orchestrator.handlers.handler_decision_store_orchestrator import (
    HandlerDecisionStoreOrchestrator,
)
from omnimarket.nodes.node_decision_store_orchestrator.models.model_decision_store_request import (
    EnumConflictSeverity,
    EnumConflictStatus,
    EnumDecisionAction,
    EnumDecisionLayer,
    EnumDecisionType,
    ModelConflictResult,
    ModelDecisionEntry,
    ModelDecisionQueryFilter,
    ModelDecisionStoreRequest,
)

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


class FakeConflictAdapter:
    def __init__(self, conflicts: list[ModelConflictResult] | None = None) -> None:
        self.conflicts = conflicts or []
        self.scopes: list[str] = []

    def check_conflicts(
        self, entry: ModelDecisionEntry, *, scope: str
    ) -> list[ModelConflictResult]:
        self.scopes.append(scope)
        return self.conflicts


class FakeSemanticAdapter:
    def __init__(self, conflicts: list[ModelConflictResult] | None = None) -> None:
        self.conflicts = conflicts
        self.calls: list[
            tuple[ModelDecisionEntry, tuple[ModelConflictResult, ...]]
        ] = []

    def review_conflicts(
        self,
        entry: ModelDecisionEntry,
        conflicts: tuple[ModelConflictResult, ...],
    ) -> list[ModelConflictResult]:
        self.calls.append((entry, conflicts))
        return self.conflicts or list(conflicts)


class FakeStoreAdapter:
    def __init__(self) -> None:
        self.persisted: list[
            tuple[ModelDecisionEntry, tuple[ModelConflictResult, ...]]
        ] = []

    def persist_decision(
        self,
        entry: ModelDecisionEntry,
        conflicts: tuple[ModelConflictResult, ...],
    ) -> str:
        self.persisted.append((entry, conflicts))
        return "decision-1"

    def query_decisions(
        self, query_filter: ModelDecisionQueryFilter | None
    ) -> dict[str, Any]:
        return {
            "entries": (
                _entry(
                    summary=f"query:{query_filter.domain if query_filter else 'all'}"
                ),
            ),
            "next_cursor": "cursor-2",
        }


class FakeNotifyAdapter:
    def __init__(self) -> None:
        self.calls: list[
            tuple[ModelDecisionEntry, tuple[ModelConflictResult, ...]]
        ] = []

    def notify_high_conflicts(
        self,
        entry: ModelDecisionEntry,
        conflicts: tuple[ModelConflictResult, ...],
    ) -> None:
        self.calls.append((entry, conflicts))


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _contract_path() -> Path:
    return _repo_root() / "src" / "omnimarket" / "nodes" / NODE_NAME / "contract.yaml"


@pytest.mark.unit
def test_contract_marks_node_implemented() -> None:
    raw = yaml.safe_load(_contract_path().read_text(encoding="utf-8"))

    assert raw["node_not_implemented"] is False
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
            "operation": "decision_store",
            "handler": {
                "name": HANDLER_CLASS,
                "module": HANDLER_MODULE,
            },
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
def test_handler_check_conflicts_is_read_only() -> None:
    conflict = _conflict(severity=EnumConflictSeverity.MEDIUM)
    adapter = FakeConflictAdapter([conflict])

    result = HandlerDecisionStoreOrchestrator(conflict_adapter=adapter).handle(
        ModelDecisionStoreRequest(
            action=EnumDecisionAction.CHECK_CONFLICTS,
            entry=_entry(),
            conflict_scope="storage",
            dry_run=True,
        )
    )

    assert result.conflicts_found == (conflict,)
    assert result.high_severity_count == 0
    assert result.slack_gate_triggered is False
    assert adapter.scopes == ["storage"]


@pytest.mark.unit
def test_handler_record_persists_without_semantic_review_for_low_confidence() -> None:
    conflict = _conflict(structural_confidence=0.5, severity=EnumConflictSeverity.LOW)
    semantic = FakeSemanticAdapter()
    store = FakeStoreAdapter()

    result = HandlerDecisionStoreOrchestrator(
        conflict_adapter=FakeConflictAdapter([conflict]),
        semantic_adapter=semantic,
        store_adapter=store,
    ).handle(
        ModelDecisionStoreRequest(
            action=EnumDecisionAction.RECORD,
            entry=_entry(),
            dry_run=False,
        )
    )

    assert result.stored_decision_id == "decision-1"
    assert semantic.calls == []
    assert len(store.persisted) == 1


@pytest.mark.unit
def test_handler_record_semantic_review_and_notify_high_conflicts() -> None:
    structural = _conflict(
        structural_confidence=0.8, severity=EnumConflictSeverity.MEDIUM
    )
    semantic_conflict = _conflict(
        structural_confidence=0.9,
        severity=EnumConflictSeverity.HIGH,
        semantic_checked=True,
    )
    semantic = FakeSemanticAdapter([semantic_conflict])
    store = FakeStoreAdapter()
    notify = FakeNotifyAdapter()

    result = HandlerDecisionStoreOrchestrator(
        conflict_adapter=FakeConflictAdapter([structural]),
        semantic_adapter=semantic,
        store_adapter=store,
        notify_adapter=notify,
    ).handle(
        ModelDecisionStoreRequest(
            action=EnumDecisionAction.RECORD,
            entry=_entry(),
            dry_run=False,
        )
    )

    assert result.conflicts_found == (semantic_conflict,)
    assert result.high_severity_count == 1
    assert result.slack_gate_triggered is True
    assert result.stored_decision_id == "decision-1"
    assert len(semantic.calls) == 1
    assert len(notify.calls) == 1


@pytest.mark.unit
def test_handler_record_dry_run_does_not_persist_or_notify() -> None:
    conflict = _conflict(structural_confidence=0.9, severity=EnumConflictSeverity.HIGH)
    store = FakeStoreAdapter()
    notify = FakeNotifyAdapter()

    result = HandlerDecisionStoreOrchestrator(
        conflict_adapter=FakeConflictAdapter([conflict]),
        store_adapter=store,
        notify_adapter=notify,
    ).handle(
        ModelDecisionStoreRequest(
            action=EnumDecisionAction.RECORD,
            entry=_entry(),
            dry_run=True,
        )
    )

    assert result.stored_decision_id is None
    assert result.high_severity_count == 1
    assert result.slack_gate_triggered is False
    assert store.persisted == []
    assert notify.calls == []


@pytest.mark.unit
def test_handler_query_uses_store_adapter() -> None:
    result = HandlerDecisionStoreOrchestrator(store_adapter=FakeStoreAdapter()).handle(
        ModelDecisionStoreRequest(
            action=EnumDecisionAction.QUERY,
            query_filter=ModelDecisionQueryFilter(domain="routing"),
            dry_run=False,
        )
    )

    assert result.query_results[0].summary == "query:routing"
    assert result.next_cursor == "cursor-2"


@pytest.mark.unit
def test_handler_requires_entry_for_record() -> None:
    with pytest.raises(ValueError, match="entry is required"):
        HandlerDecisionStoreOrchestrator().handle(
            ModelDecisionStoreRequest(
                action=EnumDecisionAction.RECORD,
                dry_run=True,
            )
        )


@pytest.mark.unit
def test_handler_requires_adapters_for_live_record() -> None:
    with pytest.raises(RuntimeError, match="conflict adapter required"):
        HandlerDecisionStoreOrchestrator().handle(
            ModelDecisionStoreRequest(
                action=EnumDecisionAction.RECORD,
                entry=_entry(),
                dry_run=False,
            )
        )

    with pytest.raises(RuntimeError, match="store adapter required"):
        HandlerDecisionStoreOrchestrator(
            conflict_adapter=FakeConflictAdapter([])
        ).handle(
            ModelDecisionStoreRequest(
                action=EnumDecisionAction.RECORD,
                entry=_entry(),
                dry_run=False,
            )
        )


@pytest.mark.unit
def test_handler_requires_semantic_adapter_for_high_confidence_conflict() -> None:
    with pytest.raises(RuntimeError, match="semantic adapter required"):
        HandlerDecisionStoreOrchestrator(
            conflict_adapter=FakeConflictAdapter(
                [
                    _conflict(
                        structural_confidence=0.7, severity=EnumConflictSeverity.MEDIUM
                    )
                ]
            ),
            store_adapter=FakeStoreAdapter(),
        ).handle(
            ModelDecisionStoreRequest(
                action=EnumDecisionAction.RECORD,
                entry=_entry(),
                dry_run=False,
            )
        )


@pytest.mark.unit
def test_handler_requires_notify_adapter_for_high_conflict() -> None:
    with pytest.raises(RuntimeError, match="notify adapter required"):
        HandlerDecisionStoreOrchestrator(
            conflict_adapter=FakeConflictAdapter(
                [
                    _conflict(
                        structural_confidence=0.5, severity=EnumConflictSeverity.HIGH
                    )
                ]
            ),
            store_adapter=FakeStoreAdapter(),
        ).handle(
            ModelDecisionStoreRequest(
                action=EnumDecisionAction.RECORD,
                entry=_entry(),
                dry_run=False,
            )
        )


def _entry(summary: str = "All APIs use Pydantic models") -> ModelDecisionEntry:
    return ModelDecisionEntry(
        decision_type=EnumDecisionType.API_CONTRACT,
        domain="api",
        layer=EnumDecisionLayer.DESIGN,
        summary=summary,
        rationale="Consistency across services.",
    )


def _conflict(
    *,
    structural_confidence: float = 0.5,
    severity: EnumConflictSeverity = EnumConflictSeverity.LOW,
    semantic_checked: bool = False,
) -> ModelConflictResult:
    return ModelConflictResult(
        conflict_id="conflict-1",
        entry_a_id="decision-a",
        entry_b_id="decision-b",
        structural_confidence=structural_confidence,
        severity=severity,
        status=EnumConflictStatus.OPEN,
        semantic_checked=semantic_checked,
    )
