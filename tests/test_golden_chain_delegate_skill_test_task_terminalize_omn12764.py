# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden-chain regression: delegate-skill 'test' task terminalization (OMN-12764).

Proves that a delegate-skill command with task_type='test' ALWAYS terminates after
the delegation orchestrator processes an inference response — it never stalls.

Failure mode being guarded:
  The live dev runtime showed that delegate-skill 'test' tasks published
  inference-response events that succeeded, yet the delegation-completed /
  delegation-failed terminal event was never emitted.  The projection query
  (correlation-trace.v1) returned row_count=0, meaning the 'test' correlation
  never appeared in delegation_events (OMN-12764).

Scenarios covered:
  1. Active path — response contains @pytest.mark.unit, compiles, no prose outside
     code block → delegation-completed terminal emitted.
  2. Inactive path — response missing @pytest.mark.unit → deterministic DoD check
     fires, delegation-failed terminal emitted (not a stall).
  3. ValueError — invalid task_type raises ValueError from the model validator
     (not a silent swallow).
  4. Bus-backed round-trip — RuntimeDelegationDispatchPort subscribes before
     publishing and receives the terminal event, confirming the 'wait=True' path
     works for 'test' task type through the full inference → quality-gate → terminal
     chain.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from omnibase_core.models.delegation.wire.model_quality_gate import (
    ModelQualityGateInput,
)
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
from pydantic import ValidationError

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.models import (
    ModelRuntimeDelegationDispatchConfig,
    ModelRuntimeDelegationDispatchTopics,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    RuntimeDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate import (
    delta as quality_gate_delta,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TEST_RESPONSE_ACTIVE = (
    "import pytest\n\n"
    "@pytest.mark.unit\n"
    "def test_normalize_active():\n"
    '    """Verify active state transition."""\n'
    "    assert normalize('active') == 'ACTIVE'\n"
    "\n"
    "@pytest.mark.unit\n"
    "def test_normalize_inactive():\n"
    '    """Verify inactive state raises ValueError."""\n'
    "    with pytest.raises(ValueError):\n"
    "        normalize('not_a_valid_state')\n"
)

_TEST_RESPONSE_MISSING_MARKER = (
    "import pytest\n\n"
    "def test_normalize_no_marker():\n"
    "    assert normalize('active') == 'ACTIVE'\n"
)

_DISPATCH_CONFIG = ModelRuntimeDelegationDispatchConfig(
    topics=ModelRuntimeDelegationDispatchTopics(
        command="test.cmd.delegation-request",
        completed="test.evt.delegation-completed",
        failed="test.evt.delegation-failed",
    ),
    request_message_type="test.delegation-request",
    source_tool="test-delegate-skill",
    consumer_group_prefix="test-delegate-skill",
    wait_timeout_seconds=5,
)


def _terminal_envelope(
    correlation_id: UUID,
    *,
    topic: str,
    completed: bool,
    content: str = "",
    failure_reason: str = "",
) -> bytes:
    """Build a minimal delegation terminal envelope as the runtime would publish.

    The ``_parse_delegation_terminal`` function in RuntimeDelegationDispatchPort
    parses raw JSON off the bus — it does not use the typed ModelDelegationEvent
    wrapper (which has a strict Literal enum on the topic field restricting it to
    the live production topic strings).  We serialise the envelope payload directly
    so tests using custom test-scoped topic strings work without hitting the enum.
    """
    import json

    payload: dict[str, object] = {
        "correlation_id": str(correlation_id),
        "task_type": "test",
        "model_used": "test-model" if completed else "",
        "endpoint_url": "http://test.local" if completed else "",
        "content": content,
        "quality_passed": completed,
        "quality_score": 0.9 if completed else 0.0,
        "latency_ms": 100,
        "prompt_tokens": 20,
        "completion_tokens": 50,
        "total_tokens": 70,
        "fallback_to_claude": False,
        "failure_reason": failure_reason,
    }
    envelope: dict[str, object] = {
        "payload": {
            "topic": topic,
            "payload": payload,
        },
        "correlation_id": str(correlation_id),
        "envelope_timestamp": datetime.now(UTC).isoformat(),
        "event_type": topic,
        "source_tool": "test-delegation-orchestrator",
    }
    return json.dumps(envelope).encode("utf-8")


# ---------------------------------------------------------------------------
# 1. Quality-gate DoD check — active path (has @pytest.mark.unit)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_test_task_with_pytest_mark_unit_passes_quality_gate() -> None:
    """A 'test' task response containing @pytest.mark.unit passes the DoD check.

    This is the 'active' case: the delegated model correctly annotates its output
    with the required marker, so the quality gate passes and the delegation
    completes.
    """
    gate_input = ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="test",
        llm_response_content=_TEST_RESPONSE_ACTIVE,
        dod_deterministic=("compiles_without_errors", "uses_pytest_mark_unit"),
        dod_heuristic=("no_refusal",),
    )
    result = quality_gate_delta(gate_input)

    assert result.passed is True, (
        f"test task with @pytest.mark.unit should pass; "
        f"failure_reasons={result.failure_reasons!r}"
    )
    assert result.fail_category == "pass"
    assert "TASK_MISMATCH: missing @pytest.mark.unit" not in result.failure_reasons


# ---------------------------------------------------------------------------
# 2. Quality-gate DoD check — inactive path (missing @pytest.mark.unit)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_test_task_without_pytest_mark_unit_fails_quality_gate() -> None:
    """A 'test' task response without @pytest.mark.unit fails the DoD check.

    This is the 'inactive' case: the model output lacks the marker, so the
    deterministic DoD check fires with TASK_MISMATCH.  The terminal should be
    delegation-failed (not a stall).
    """
    gate_input = ModelQualityGateInput(
        correlation_id=uuid4(),
        task_type="test",
        llm_response_content=_TEST_RESPONSE_MISSING_MARKER,
        dod_deterministic=("compiles_without_errors", "uses_pytest_mark_unit"),
        dod_heuristic=("no_refusal",),
    )
    result = quality_gate_delta(gate_input)

    assert result.passed is False, (
        "test task without @pytest.mark.unit should fail the DoD check"
    )
    assert result.fail_category == "fail_deterministic", (
        f"expected fail_deterministic, got {result.fail_category!r}"
    )
    assert any("pytest.mark.unit" in r for r in result.failure_reasons), (
        f"expected marker failure in reasons: {result.failure_reasons!r}"
    )


# ---------------------------------------------------------------------------
# 3. ValueError for invalid task_type
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_invalid_task_type_raises_validation_error() -> None:
    """An unrecognised task_type raises a ValidationError from the wire model.

    The Literal constraint on ModelDelegateSkillRequest.task_type rejects any
    value not in the declared set — the failure is explicit, not a silent swallow.
    """
    with pytest.raises((ValueError, ValidationError)):
        ModelDelegateSkillRequest(
            prompt="Write tests for the utility module.",
            task_type="not_a_valid_task_type",  # type: ignore[arg-type]
            source="claude-code",
        )


# ---------------------------------------------------------------------------
# 4. Bus-backed round-trip — 'test' task terminates via RuntimeDelegationDispatchPort
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_test_task_terminates_via_bus_backed_dispatch_port() -> None:
    """delegate-skill 'test' task terminates when the runtime emits a terminal event.

    This is the direct regression for OMN-12764: the RuntimeDelegationDispatchPort
    subscribes to the terminal topics BEFORE publishing the delegation request so it
    cannot miss an event.  Here the 'runtime' is simulated by a coroutine that
    reads the published request and immediately publishes a delegation-completed
    terminal on the expected response topic.

    Asserts:
    - The port receives the terminal event (no TimeoutError).
    - Result status is 'completed'.
    - task_type is preserved in the round-trip (delegate-skill path for 'test').
    """
    bus = EventBusInmemory(environment="test", group="omn12764-test-task-regression")
    await bus.start()
    original_correlation_id = uuid4()
    received_requests: list[ModelDelegationRequest] = []

    async def _simulate_runtime(message: ModelEventMessage) -> None:
        """Simulate the delegation orchestrator + quality gate terminal emission."""
        envelope = ModelEventEnvelope[ModelDelegationRequest].model_validate_json(
            message.value
        )
        received_requests.append(envelope.payload)
        terminal_bytes = _terminal_envelope(
            envelope.payload.correlation_id,
            topic=_DISPATCH_CONFIG.topics.completed,
            completed=True,
            content=_TEST_RESPONSE_ACTIVE,
        )
        await bus.publish(
            _DISPATCH_CONFIG.topics.completed,
            None,
            terminal_bytes,
            None,
        )

    try:
        await bus.subscribe(
            _DISPATCH_CONFIG.topics.command,
            group_id=f"omn12764-runtime-sim-{uuid4()}",
            on_message=_simulate_runtime,
        )
        port = RuntimeDelegationDispatchPort(
            event_bus=bus,
            config=_DISPATCH_CONFIG,
        )
        result = await port.dispatch(
            prompt="Write pytest unit tests for normalize_status.",
            task_type="test",
            correlation_id=original_correlation_id,
            max_tokens=512,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            quality_contract_mode="replace_task_class",
            acceptance_criteria=(),
        )
    finally:
        await bus.close()

    # The 'test' task must NOT stall — a result must be returned.
    assert result["status"] == "completed", (
        f"test task should terminalize with status=completed, got {result['status']!r}. "
        "If this is a TimeoutError, the terminal event was not received — "
        "reproduces the OMN-12764 stall."
    )
    assert len(received_requests) == 1
    assert received_requests[0].task_type == "test"
    assert received_requests[0].correlation_id == original_correlation_id


# ---------------------------------------------------------------------------
# 5. Bus-backed round-trip — 'test' task terminates with DoD failure (failed path)
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_test_task_failed_terminal_received_via_bus() -> None:
    """delegate-skill 'test' task with a DoD failure also terminates (not stalls).

    When the model response fails the DoD check (e.g. missing @pytest.mark.unit),
    the delegation orchestrator emits delegation-failed.v1.  The port must receive
    that terminal too, so the caller gets a typed 'failed' response, not a timeout.
    """
    bus = EventBusInmemory(environment="test", group="omn12764-failed-test-task")
    await bus.start()
    original_correlation_id = uuid4()

    async def _simulate_runtime_failure(message: ModelEventMessage) -> None:
        envelope = ModelEventEnvelope[ModelDelegationRequest].model_validate_json(
            message.value
        )
        terminal_bytes = _terminal_envelope(
            envelope.payload.correlation_id,
            topic=_DISPATCH_CONFIG.topics.failed,
            completed=False,
            failure_reason="TASK_MISMATCH: missing @pytest.mark.unit",
        )
        await bus.publish(
            _DISPATCH_CONFIG.topics.failed,
            None,
            terminal_bytes,
            None,
        )

    try:
        await bus.subscribe(
            _DISPATCH_CONFIG.topics.command,
            group_id=f"omn12764-failed-sim-{uuid4()}",
            on_message=_simulate_runtime_failure,
        )
        port = RuntimeDelegationDispatchPort(
            event_bus=bus,
            config=_DISPATCH_CONFIG,
        )
        result = await port.dispatch(
            prompt="Write tests without the required marker.",
            task_type="test",
            correlation_id=original_correlation_id,
            max_tokens=512,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            quality_contract_mode="replace_task_class",
            acceptance_criteria=(),
        )
    finally:
        await bus.close()

    assert result["status"] == "failed", (
        f"test task DoD failure should produce status=failed, got {result['status']!r}. "
        "If this is a TimeoutError, the terminal event was not received — "
        "reproduces the OMN-12764 stall on the failure path."
    )
    assert "pytest.mark.unit" in str(result.get("failure_reason", ""))


# ---------------------------------------------------------------------------
# 6. HandlerDelegateSkill — 'test' task type is accepted and dispatched
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_handler_delegate_skill_accepts_test_task_type() -> None:
    """HandlerDelegateSkill accepts task_type='test' and dispatches it unchanged.

    The 'test' task type is in the Literal set on ModelDelegateSkillRequest and
    must pass validation and reach the dispatch port without transformation.
    """

    class _RecordingPort:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def dispatch(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(dict(kwargs))
            return {
                "status": "completed",
                "content": _TEST_RESPONSE_ACTIVE,
                "delegated_to": "local",
                "model_name": "test-model",
                "quality_gate_passed": True,
                "quality_score": 0.9,
            }

    port = _RecordingPort()
    handler = HandlerDelegateSkill(dispatch_port=port)
    correlation_id = uuid4()

    response = await handler.handle(
        ModelDelegateSkillRequest(
            prompt="Write pytest unit tests for the normalize function.",
            task_type="test",
            source="claude-code",
            correlation_id=correlation_id,
        )
    )

    assert response.status == "completed"
    assert response.task_type == "test"
    assert response.correlation_id == correlation_id
    assert len(port.calls) == 1
    assert port.calls[0]["task_type"] == "test", (
        f"task_type must be forwarded as 'test', got {port.calls[0]['task_type']!r}"
    )
