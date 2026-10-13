# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runtime-entry proof for node_adversarial_pipeline_orchestrator [OMN-21055].

A start command run the way the deployed runtime runs it must reach exactly one
contract terminal: ``adversarial-pipeline-completed`` when the review gate
passes, ``adversarial-pipeline-gate-failed`` when it does not.

The deployed runtime publishes a handler's returned model through the contract
result applier (``build_contract_result_applier``): the success terminal by
default, the one declared failure terminal when the model states a failure
verdict. These tests drive that applier over the REAL contract file with the
REAL handler's result, so a contract that declares the gate-failed topic but
cannot reach it is caught here rather than on the bus.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
import yaml
from omnibase_infra.enums import EnumDispatchStatus
from omnibase_infra.models.dispatch.model_dispatch_result import ModelDispatchResult
from omnibase_infra.protocols import ProtocolEventBusLike
from omnibase_infra.runtime.contract_terminal_events import (
    declared_failure_terminal_topics,
    resolve_terminal_verdict,
)
from omnibase_infra.runtime.service_dispatch_result_applier import (
    build_contract_result_applier,
)
from pydantic import BaseModel

from omnimarket.adapters.codex.local_runtime_dispatch import LocalRuntimeDispatch
from omnimarket.adapters.codex.runtime_client import ModelDispatchBusCommand
from omnimarket.nodes.node_adversarial_pipeline_orchestrator.handlers.handler_adversarial_pipeline_orchestrator import (
    HandlerAdversarialPipelineOrchestrator,
)
from omnimarket.nodes.node_adversarial_pipeline_orchestrator.models.model_adversarial_pipeline_request import (
    ModelAdversarialPipelineRequest,
    ModelAdversarialPipelineResult,
)

_NODE_NAME = "node_adversarial_pipeline_orchestrator"
_COMMAND = "onex.cmd.omnimarket.adversarial-pipeline-start.v1"
_COMPLETED = "onex.evt.omnimarket.adversarial-pipeline-completed.v1"
_GATE_FAILED = "onex.evt.omnimarket.adversarial-pipeline-gate-failed.v1"
_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / _NODE_NAME
    / "contract.yaml"
)


class _Design:
    def create_plan(self, payload: dict[str, Any]) -> dict[str, str]:
        return {"plan_path": "/tmp/synthetic-plan.md"}


class _Review:
    def __init__(self, findings_count: int) -> None:
        self.findings_count = findings_count

    def review_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "findings_count": self.findings_count,
            "findings_summary": f"{self.findings_count} findings",
        }


class _Tickets:
    def __init__(self) -> None:
        self.calls = 0

    def create_tickets(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        return {"created_ticket_ids": ("OMN-1",), "tickets_created": 1}


def _run_dry_start(
    findings_count: int, tickets: _Tickets
) -> ModelAdversarialPipelineResult:
    return HandlerAdversarialPipelineOrchestrator(
        design_adapter=_Design(),
        review_adapter=_Review(findings_count),
        ticket_adapter=tickets,
    ).handle(
        ModelAdversarialPipelineRequest(
            topic="synthetic probe", dry_run=True, no_launch=True
        )
    )


def _applier() -> Any:
    return build_contract_result_applier(
        event_bus=AsyncMock(spec=ProtocolEventBusLike),
        contract_path=_CONTRACT_PATH,
        publish_topics=[_COMPLETED, _GATE_FAILED],
        terminal_event=_COMPLETED,
    )


async def _published_topic(applier: Any, correlation_id: UUID, event: BaseModel) -> str:
    await applier.apply(
        ModelDispatchResult(
            status=EnumDispatchStatus.SUCCESS,
            topic=_COMMAND,
            started_at=datetime.now(UTC),
            correlation_id=correlation_id,
            dispatcher_id="omn21055-runtime-seam",
            output_events=[event],
        ),
        correlation_id=correlation_id,
    )
    bus = applier._event_bus
    bus.publish_envelope.assert_awaited_once()
    return str(bus.publish_envelope.await_args.kwargs["topic"])


@pytest.mark.unit
def test_contract_declares_gate_failed_as_the_one_failure_terminal() -> None:
    contract = yaml.safe_load(_CONTRACT_PATH.read_text(encoding="utf-8"))

    assert contract["terminal_event"] == _COMPLETED
    assert declared_failure_terminal_topics(
        _CONTRACT_PATH,
        success_topic=_COMPLETED,
        publishable_topics=contract["event_bus"]["publish_topics"],
    ) == (_GATE_FAILED,)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_below_gate_start_publishes_one_gate_failed_terminal() -> None:
    tickets = _Tickets()
    result = _run_dry_start(findings_count=1, tickets=tickets)
    correlation_id = uuid4()

    assert result.gate_passed is False
    assert resolve_terminal_verdict(result) is False
    assert await _published_topic(_applier(), correlation_id, result) == _GATE_FAILED
    assert tickets.calls == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_gate_pass_publishes_one_completed_terminal_and_no_tickets() -> (
    None
):
    tickets = _Tickets()
    result = _run_dry_start(findings_count=4, tickets=tickets)
    correlation_id = uuid4()

    assert result.gate_passed is True
    assert result.dry_run is True
    assert result.tickets_created == 0
    assert resolve_terminal_verdict(result) is True
    assert await _published_topic(_applier(), correlation_id, result) == _COMPLETED
    assert tickets.calls == 0


@pytest.mark.unit
def test_result_status_follows_the_gate_and_cannot_contradict_it() -> None:
    assert ModelAdversarialPipelineResult(gate_passed=False, dry_run=True).status == (
        "failed"
    )
    assert ModelAdversarialPipelineResult(gate_passed=True, dry_run=True).status == (
        "completed"
    )
    with pytest.raises(ValueError, match="contradicts gate_passed"):
        ModelAdversarialPipelineResult(
            gate_passed=False, dry_run=True, status="completed"
        )
    with pytest.raises(ValueError, match="contradicts gate_passed"):
        ModelAdversarialPipelineResult(gate_passed=True, dry_run=True, status="failed")

    wire = ModelAdversarialPipelineResult(gate_passed=False, dry_run=True)
    assert ModelAdversarialPipelineResult.model_validate(wire.model_dump()) == wire


@pytest.mark.asyncio
@pytest.mark.unit
async def test_unwired_stage_adapters_are_refused_by_name(tmp_path: Path) -> None:
    """The runtime builds the handler with no stage adapters when none are
    registered; the caller must get a named failure, not silence."""
    runtime = LocalRuntimeDispatch(
        adapter_command_topic="onex.cmd.codex.pattern-b-dispatch.v1",
        state_root=tmp_path / "state",
    )
    terminal, evidence = await runtime.dispatch(
        ModelDispatchBusCommand(
            command_name="adversarial_pipeline_orchestrator",
            requester="pytest",
            payload={"topic": "synthetic probe", "dry_run": True, "no_launch": True},
            correlation_id=uuid4(),
            response_topic="onex.evt.codex.pattern-b-dispatch-completed.v1",
            timeout_seconds=5.0,
        )
    )

    assert evidence.node_name == _NODE_NAME
    assert evidence.command_topic == _COMMAND
    assert terminal.status == "failed"
    assert terminal.payload is not None
    assert "design adapter required" in str(terminal.payload.get("error_message"))
