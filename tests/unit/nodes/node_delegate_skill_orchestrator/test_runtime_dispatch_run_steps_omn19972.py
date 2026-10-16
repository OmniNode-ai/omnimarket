# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19972: the deployed delegation path publishes the run's numbered steps.

Before this, only the bus-less local port built a ``RunStepRecorder``. A run
through the deployed chain (``RuntimeDelegationDispatchPort``) published its
request and read its terminal, and nothing reached
``onex.evt.omnimarket.delegation-run-step.v1``, so the run-trace writer had
nothing to fold. These tests drive the real port over an in-memory bus with a
real V2 terminal and read the step topic the writer subscribes to, decoding
each message the way the writer does.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from omnibase_core.models.delegation.wire.model_delegation_terminal_v2 import (
    ModelDelegationTerminalCompletedV2,
    ModelDelegationTerminalFailedRoutedV2,
    ModelDelegationTerminalFailedUnroutedV2,
)
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
from pydantic import BaseModel

from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_runtime_delegation_dispatch import (
    RuntimeDelegationDispatchPort,
    load_runtime_delegation_dispatch_config,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.run_step_recorder import (
    run_step_topic,
)
from omnimarket.nodes.node_projection_run_trace.models import ModelDelegationRunStep
from omnimarket.projection.envelope import (
    strip_runner_injected_keys,
    unwrap_envelope,
)

_LADDER: tuple[dict[str, object], ...] = (
    {
        "tier": "local",
        "backend_ref": "local-chat-a",
        "model_used": "model-a",
        "acceptance_decision": "climb",
        "acceptance_reason": "provider_call_failed",
        "failure_reasons": ["provider call failed"],
    },
    {
        "tier": "local",
        "backend_ref": "local-chat-b",
        "model_used": "model-b",
        "acceptance_decision": "accept",
        "acceptance_reason": "quality_bar_met",
        "quality_score": 0.95,
    },
)


def _common(correlation_id: UUID, *, outcome: str) -> dict[str, Any]:
    return {
        "correlation_id": correlation_id,
        "task_type": "document",
        "model_used": "model-b",
        "endpoint_url": "http://local-runtime.invalid:1234/v1/chat/completions",
        "content": "answer",
        "latency_ms": 40,
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "total_tokens": 5,
        "fallback_to_claude": False,
        "failure_reason": "",
        "tokens_to_compliance": 0,
        "compliance_attempts": 1,
        "escalation_count": 1,
        "escalation_history": _LADDER,
        "routing_tiers_hash": "test-routing",
        "escalation_config_hash": "test-escalation",
        "attempts_count": 2,
        "cumulative_attempt_cost": 0.0,
        "cumulative_input_tokens": 3,
        "cumulative_output_tokens": 2,
        "final_attempt_cost": 0.0,
        "context_pack_hash": "",
        "cost_tier_name": "local",
        "tenant_id": "tenant-from-terminal",
        "terminal_outcome": outcome,
    }


def _routed() -> dict[str, Any]:
    return {
        "routing_disposition": "routed",
        "backend_ref": "local-chat-b",
        "pricing_manifest_version": 1,
        "quality_bar_evaluation": {
            "quality_score": 0.95,
            "required_quality_bar": 0.8,
            "score_vs_required_bar": "at_or_above_bar",
        },
        "failed_acceptance_criteria": (),
    }


async def _run(
    terminal: Callable[[UUID], BaseModel] | None,
    *,
    tenant_id: str | None = "tenant-from-request",
) -> tuple[UUID, dict[str, object], list[ModelDelegationRunStep]]:
    correlation_id = uuid4()
    config = load_runtime_delegation_dispatch_config().model_copy(
        update={"wait_timeout_seconds": 1}
    )
    topic = run_step_topic()
    assert topic == "onex.evt.omnimarket.delegation-run-step.v1"
    raw_steps: list[bytes] = []
    bus = EventBusInmemory(environment="test", group="run-steps-19972")
    await bus.start()

    async def on_step(message: ModelEventMessage) -> None:
        raw_steps.append(message.value)

    async def on_command(message: ModelEventMessage) -> None:
        if terminal is None:
            return
        built = terminal(correlation_id)
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=built.model_dump(mode="json"),
            correlation_id=correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type="test",
            source_tool="run-steps-19972",
        )
        out = (
            config.topics.completed
            if isinstance(built, ModelDelegationTerminalCompletedV2)
            else config.topics.failed_unrouted
            if isinstance(built, ModelDelegationTerminalFailedUnroutedV2)
            else config.topics.failed
        )
        assert out is not None
        await bus.publish(out, None, envelope.model_dump_json().encode(), None)

    try:
        await bus.subscribe(topic, group_id="run-trace-writer", on_message=on_step)
        await bus.subscribe(
            config.topics.command, group_id="chain", on_message=on_command
        )
        port = RuntimeDelegationDispatchPort(event_bus=bus, config=config)
        result = await port.dispatch(
            prompt="p",
            task_type="document",
            correlation_id=correlation_id,
            max_tokens=None,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=30,
            terminal_delivery_margin_seconds=5,
            quality_contract_mode="replace_task_class",
            acceptance_criteria=(),
            tenant_id=tenant_id,
        )
        await asyncio.sleep(0)
    finally:
        await bus.close()
    steps = []
    for raw in raw_steps:
        data = unwrap_envelope(raw)
        assert data is not None
        # The writer's own decode: unwrap, strip the runner keys, validate.
        steps.append(
            ModelDelegationRunStep.model_validate(strip_runner_injected_keys(data))
        )
    return correlation_id, result, steps


@pytest.mark.unit
async def test_completed_bus_run_publishes_each_numbered_step() -> None:
    def build(cid: UUID) -> ModelDelegationTerminalCompletedV2:
        return ModelDelegationTerminalCompletedV2(
            **_common(cid, outcome="completed"), **_routed(), quality_passed=True
        )

    correlation_id, result, steps = await _run(build)

    assert result["status"] == "completed"
    assert [s.step_kind.value for s in steps] == [
        "request",
        "routing_decision",
        "provider_call",
        "routing_decision",
        "provider_call",
        "quality_gate",
        "terminal",
    ]
    assert [s.step_sequence for s in steps] == list(range(1, 8))
    assert {s.correlation_id for s in steps} == {correlation_id}
    # The terminal's tenant is the one the run was billed under.
    assert {s.tenant_id for s in steps} == {"tenant-from-terminal"}
    first_call, second_call = steps[2], steps[4]
    assert first_call.outcome.value == "failed"
    assert first_call.failure_cause == "provider_call_failed"
    assert first_call.backend_id == "local-chat-a"
    assert second_call.outcome.value == "succeeded"
    assert second_call.backend_id == "local-chat-b"
    assert second_call.model_id == "model-b"
    assert second_call.duration_ms == 40
    assert steps[5].outcome.value == "succeeded"
    assert steps[6].outcome.value == "succeeded"
    assert steps[6].backend_id == "local-chat-b"


@pytest.mark.unit
async def test_failed_routed_run_names_the_failing_step() -> None:
    def build(cid: UUID) -> ModelDelegationTerminalFailedRoutedV2:
        common = _common(cid, outcome="failed")
        common["escalation_history"] = ()
        common["escalation_count"] = 0
        common["attempts_count"] = 1
        routed = _routed()
        routed["quality_bar_evaluation"] = {
            "quality_score": 0.2,
            "required_quality_bar": 0.8,
            "score_vs_required_bar": "below_bar",
        }
        return ModelDelegationTerminalFailedRoutedV2(
            **common,
            **routed,
            quality_passed=False,
            terminal_failure_reason="quality gate refused",
            routed_failure_cause={"kind": "quality_gate_rejection"},
        )

    _, result, steps = await _run(build)

    assert result["status"] == "failed"
    assert [s.step_kind.value for s in steps] == [
        "request",
        "routing_decision",
        "provider_call",
        "quality_gate",
        "terminal",
    ]
    gate, terminal = steps[3], steps[4]
    assert gate.outcome.value == "failed"
    assert gate.failure_cause == "quality_gate_rejection"
    assert terminal.outcome.value == "failed"
    assert terminal.failing_step_sequence == gate.step_sequence


@pytest.mark.unit
async def test_unrouted_run_publishes_request_and_failed_terminal() -> None:
    def build(cid: UUID) -> ModelDelegationTerminalFailedUnroutedV2:
        common = _common(cid, outcome="failed")
        common["escalation_history"] = ()
        common["escalation_count"] = 0
        common["attempts_count"] = 1
        return ModelDelegationTerminalFailedUnroutedV2(
            **common,
            routing_disposition="unrouted",
            unrouted_reason="no_eligible_backend",
            terminal_failure_reason="no eligible backend",
        )

    _, _, steps = await _run(build)

    assert [s.step_kind.value for s in steps] == ["request", "terminal"]
    assert steps[1].outcome.value == "failed"
    assert steps[1].failure_cause == "no_eligible_backend"


@pytest.mark.unit
async def test_timed_out_run_publishes_a_failed_terminal_under_the_request_tenant() -> (
    None
):
    _, result, steps = await _run(None)

    assert result["status"] == "timeout"
    assert [s.step_kind.value for s in steps] == ["request", "terminal"]
    assert {s.tenant_id for s in steps} == {"tenant-from-request"}
    assert steps[1].failure_cause == "timeout"


@pytest.mark.unit
async def test_a_run_with_no_tenant_anywhere_publishes_no_step() -> None:
    # A step that cannot say whose run it is is refused by the model, never
    # filed under a guessed tenant; the delegation itself still answers.
    _, result, steps = await _run(None, tenant_id=None)

    assert result["status"] == "timeout"
    assert steps == []
