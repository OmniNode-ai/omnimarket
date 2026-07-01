# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Multi-parameter bus round-trip integration test for node_session_orchestrator.

WS-5 Wave 7 (OMN-13681), extended under OMN-13784 for full declared-state
coverage of ``EnumSessionStatus`` (complete/halted/error) and the Phase
1->2->3 control-flow graph. ORCHESTRATOR archetype -> Variant B: the handler
is registered on ``EventBusInmemory`` via ``LocalRuntimeBusAdapter``; a start
command is published and the terminal event on the completion topic is
asserted.

The I/O boundary (the 8 health-gate probes + Linear) is mocked via injected
collaborators: health probes are constructor-injected ``ProbeCallable``s, and
Phase 2 RSD scoring reads a deterministic Linear fixture file. No live Kafka,
no SSH, no .201 — fully in-process.

Param axes (>=3 distinct sets + a negative control):
  * Phase 1 all-GREEN gate -> PROCEED / complete.
  * Phase 1 RED blocking dimension -> FIX_ONLY / halted  (NEGATIVE CONTROL).
  * Phase 1 YELLOW non-blocking -> PROCEED / complete, overall YELLOW.
  * skip_health bypass -> complete with no health_report.
  * Phase 2 RSD scoring from a Linear fixture -> ordered dispatch_queue.

Additional declared-state closure (OMN-13784):
  * Phase 2 Linear fetch failure (empty-fixture edge case) -> ``status=error``,
    the third ``EnumSessionStatus`` member the base multiparam matrix above
    never reaches.
  * Full pipeline fallthrough (phase=0, dry_run) -> Phase 3 dispatch runs and
    populates ``dispatch_receipts``, closing the only control-flow edge (the
    phase-3 fallthrough after phase 1+2 succeed) the ``phase=1``/``phase=2``
    early-return cases above never exercise.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_session_orchestrator.handlers.handler_session_orchestrator import (
    TOPIC_SESSION_ORCH_COMPLETED,
    TOPIC_SESSION_ORCH_START,
    EnumDimensionStatus,
    HandlerSessionOrchestrator,
    ModelHealthDimensionResult,
    ModelSessionOrchestratorCommand,
)
from tests.integration._wave7_bus import drive_round_trip


def _probe(
    dimension: str,
    status: EnumDimensionStatus,
    *,
    blocks: bool,
) -> Any:
    """Build a constructor-injectable health probe returning a fixed dimension."""

    def _call() -> ModelHealthDimensionResult:
        return ModelHealthDimensionResult(
            dimension=dimension,
            status=status,
            source="injected_mock",
            timestamp=datetime.now(tz=UTC),
            stale_after=timedelta(minutes=5),
            details={"mock": True},
            actionable_items=[] if status == EnumDimensionStatus.GREEN else ["fix it"],
            blocks_dispatch=blocks,
        )

    return _call


_ALL_GREEN = [
    _probe("pr_inventory", EnumDimensionStatus.GREEN, blocks=False),
    _probe("runtime_health", EnumDimensionStatus.GREEN, blocks=False),
]
_RED_BLOCKING = [
    _probe("pr_inventory", EnumDimensionStatus.GREEN, blocks=False),
    _probe("runtime_health", EnumDimensionStatus.RED, blocks=True),
]
_YELLOW_NONBLOCKING = [
    _probe("pr_inventory", EnumDimensionStatus.GREEN, blocks=False),
    _probe("observability", EnumDimensionStatus.YELLOW, blocks=False),
]


def _write_linear_fixture(tmp_path: Path) -> str:
    """Two Active-Sprint tickets with distinct priorities for deterministic order."""
    nodes = [
        {
            "id": "id-low",
            "identifier": "OMN-LOW",
            "title": "low priority",
            "priority": 4,  # Low -> acceleration 1.0
            "labels": [],
            "updatedAt": "2020-01-01T00:00:00Z",
            "children": [],
        },
        {
            "id": "id-urgent",
            "identifier": "OMN-URGENT",
            "title": "urgent",
            "priority": 1,  # Urgent -> acceleration 4.0
            "labels": [],
            "updatedAt": "2020-01-01T00:00:00Z",
            "children": [],
        },
    ]
    path = tmp_path / "linear_fixture.json"
    path.write_text(json.dumps(nodes), encoding="utf-8")
    return str(path)


# (case_id, probes, command_kwargs, phase2_fixture, expectations)
_CASES: list[tuple[str, list[Any], dict[str, Any], bool, dict[str, Any]]] = [
    (
        "phase1-all-green-proceed",
        _ALL_GREEN,
        {"phase": 1},
        False,
        {"status": "complete", "gate_decision": "PROCEED", "overall": "GREEN"},
    ),
    (
        "phase1-red-blocking-halts",  # NEGATIVE CONTROL
        _RED_BLOCKING,
        {"phase": 1},
        False,
        {"status": "halted", "gate_decision": "FIX_ONLY", "overall": "RED"},
    ),
    (
        "phase1-yellow-nonblocking-proceeds",
        _YELLOW_NONBLOCKING,
        {"phase": 1},
        False,
        {"status": "complete", "gate_decision": "PROCEED", "overall": "YELLOW"},
    ),
    (
        "skip-health-bypass",
        _RED_BLOCKING,  # would block, but skip_health ignores the gate
        {"phase": 1, "skip_health": True},
        False,
        {"status": "complete", "health_report_is_none": True},
    ),
    (
        "phase2-rsd-ordered-queue",
        _ALL_GREEN,
        {"phase": 2, "dry_run": True},
        True,
        {"status": "complete", "queue_len": 2, "queue_first": "OMN-URGENT"},
    ),
]


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_id", "probes", "command_kwargs", "phase2_fixture", "expect"),
    _CASES,
    ids=[c[0] for c in _CASES],
)
async def test_session_orchestrator_round_trip(
    integration_event_bus: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case_id: str,
    probes: list[Any],
    command_kwargs: dict[str, Any],
    phase2_fixture: bool,
    expect: dict[str, Any],
) -> None:
    if phase2_fixture:
        monkeypatch.setenv(
            "ONEX_SESSION_ORCHESTRATOR_LINEAR_FIXTURE",
            _write_linear_fixture(tmp_path),
        )

    handler = HandlerSessionOrchestrator(probes=probes)
    command = ModelSessionOrchestratorCommand(
        correlation_id="corr-wave7",
        session_id="sess-wave7",
        state_dir=str(tmp_path / "state"),
        **command_kwargs,
    )

    history = await drive_round_trip(
        integration_event_bus,
        handler=handler,
        handler_name="session-orchestrator",
        input_model_cls=ModelSessionOrchestratorCommand,
        start_topic=TOPIC_SESSION_ORCH_START,
        output_topic=TOPIC_SESSION_ORCH_COMPLETED,
        payload_bytes=command.model_dump_json().encode("utf-8"),
        group_id=f"session-orch-test-{case_id}",
    )

    assert len(history) == 1, f"{case_id}: expected exactly one terminal event"
    payload = json.loads(history[0].value)

    assert payload["correlation_id"] == "corr-wave7"
    assert payload["session_id"] == "sess-wave7"
    assert payload["status"] == expect["status"]
    assert "onex.evt.omnimarket.session-health-transition.v1"

    if expect.get("health_report_is_none"):
        assert payload["health_report"] is None
    if "gate_decision" in expect:
        assert payload["health_report"]["gate_decision"] == expect["gate_decision"]
        assert payload["health_report"]["overall_status"] == expect["overall"]
        # Every dimension we injected must be present in the report.
        assert len(payload["health_report"]["dimensions"]) == len(probes)

    if expect["status"] in ("halted", "error"):
        assert payload["halt_reason"], (
            f"{expect['status']} result must carry a halt_reason"
        )
    else:
        assert payload["halt_reason"] == ""

    if "queue_len" in expect:
        assert len(payload["dispatch_queue"]) == expect["queue_len"]
        assert payload["dispatch_queue"][0] == expect["queue_first"]


def _write_empty_linear_fixture(tmp_path: Path) -> str:
    """A present-but-empty fixture — Phase 2 treats this as an unrecoverable fetch failure."""
    path = tmp_path / "linear_fixture_empty.json"
    path.write_text(json.dumps([]), encoding="utf-8")
    return str(path)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_orchestrator_phase2_fetch_failure_yields_error_status(
    integration_event_bus: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closes the third ``EnumSessionStatus`` member: ``error``.

    An empty Linear fixture makes Phase 2 raise ``SessionLinearFetchError``
    (distinguishing "fetch failed" from "legitimately zero tickets"). The
    handler catches this and converts it to a terminal ``status=error``
    result rather than letting the exception escape the bus boundary.
    """
    monkeypatch.setenv(
        "ONEX_SESSION_ORCHESTRATOR_LINEAR_FIXTURE",
        _write_empty_linear_fixture(tmp_path),
    )
    handler = HandlerSessionOrchestrator(probes=_ALL_GREEN)
    command = ModelSessionOrchestratorCommand(
        correlation_id="corr-wave7-error",
        session_id="sess-wave7-error",
        state_dir=str(tmp_path / "state"),
        phase=2,
    )

    history = await drive_round_trip(
        integration_event_bus,
        handler=handler,
        handler_name="session-orchestrator",
        input_model_cls=ModelSessionOrchestratorCommand,
        start_topic=TOPIC_SESSION_ORCH_START,
        output_topic=TOPIC_SESSION_ORCH_COMPLETED,
        payload_bytes=command.model_dump_json().encode("utf-8"),
        group_id="session-orch-test-phase2-error",
    )

    assert len(history) == 1
    payload = json.loads(history[0].value)
    assert payload["status"] == "error"
    assert "Phase 2" in payload["halt_reason"]
    assert payload["dispatch_queue"] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_session_orchestrator_full_pipeline_reaches_phase3_dispatch(
    integration_event_bus: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closes the phase-3 fallthrough control-flow edge.

    ``phase=1``/``phase=2`` cases above early-return before Phase 3 ever
    runs. The default ``phase=0`` (unset) falls through all three phases;
    this proves the pipeline reaches Phase 3 and populates
    ``dispatch_receipts`` with the compiled (dry-run) dispatch specs.
    """
    monkeypatch.setenv(
        "ONEX_SESSION_ORCHESTRATOR_LINEAR_FIXTURE",
        _write_linear_fixture(tmp_path),
    )
    handler = HandlerSessionOrchestrator(probes=_ALL_GREEN)
    command = ModelSessionOrchestratorCommand(
        correlation_id="corr-wave7-phase3",
        session_id="sess-wave7-phase3",
        state_dir=str(tmp_path / "state"),
        dry_run=True,
    )

    history = await drive_round_trip(
        integration_event_bus,
        handler=handler,
        handler_name="session-orchestrator",
        input_model_cls=ModelSessionOrchestratorCommand,
        start_topic=TOPIC_SESSION_ORCH_START,
        output_topic=TOPIC_SESSION_ORCH_COMPLETED,
        payload_bytes=command.model_dump_json().encode("utf-8"),
        group_id="session-orch-test-phase3-dispatch",
    )

    assert len(history) == 1
    payload = json.loads(history[0].value)
    assert payload["status"] == "complete"
    assert len(payload["dispatch_queue"]) == 2
    assert len(payload["dispatch_receipts"]) == 2
    for raw_receipt in payload["dispatch_receipts"]:
        receipt = json.loads(raw_receipt)
        assert receipt["status"] == "dry_run"
        assert receipt["ticket_id"] in payload["dispatch_queue"]
