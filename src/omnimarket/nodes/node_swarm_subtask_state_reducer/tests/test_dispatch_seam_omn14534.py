# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Cross-boundary seam test for OMN-14534.

Drives the ACTUAL real producer wire models (not a hand-rolled stand-in)
through ``HandlerSwarmSubtaskState.handle()`` — the exact call the runtime's
auto-wiring dispatcher makes once contract.yaml declares a per-topic
``event_model`` (omnibase_infra handler_wiring.py: ``handle_method(typed_payload)``
where ``typed_payload`` is an instance of the declared event_model class).

Before OMN-14534, contract.yaml had NO event_model, so the dispatcher instead
called ``handle_method(envelope)`` with the raw ``ModelEventEnvelope`` object,
and ``handle()`` did ``ModelSwarmSubtaskReducerInput(**input_data)`` — a
TypeError on any non-mapping input, silently swallowed by
MessageDispatchEngine into HANDLER_ERROR while the offset still committed.

RED proof (below): even a validated, spec-shaped ``ModelDelegationEvent``
never arrives on the wire — the 5 real producer models this reducer actually
subscribes to have a fundamentally different field set (no run_id/subtask_id/
event_type at all). Constructing ``ModelDelegationEvent`` directly from any of
the real producers' ``model_dump()`` fails validation, proving the two shapes
are genuinely incompatible and an adapter (not just an event_model
declaration) was required.

GREEN proof: ``handle()`` now accepts each real producer model directly and
produces the correct FSM transition.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from omnimarket.enums.enum_cost_basis import EnumCostBasis
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.enums.enum_usage_source import EnumUsageSource
from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_all_tiers_failed_event import (
    ModelLlmDelegationAllTiersFailedEvent,
)
from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_completed_event import (
    ModelLlmDelegationCompletedEvent,
)
from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_escalation_triggered_event import (
    ModelLlmDelegationEscalationTriggeredEvent,
)
from omnimarket.nodes.node_llm_delegation_call_effect.models.model_llm_delegation_call_request import (
    ModelLlmDelegationCallRequest,
)
from omnimarket.nodes.node_swarm_fanout_orchestrator.models.model_swarm_fanout_result import (
    ModelSwarmFanoutResult,
)
from omnimarket.nodes.node_swarm_subtask_state_reducer.handlers.handler_swarm_subtask_state import (
    HandlerSwarmSubtaskState,
)
from omnimarket.nodes.node_swarm_subtask_state_reducer.models.model_subtask_state import (
    EnumSubtaskState,
)
from omnimarket.nodes.node_swarm_subtask_state_reducer.models.model_swarm_subtask_input import (
    ModelDelegationEvent,
)

_RUN_ID = "swarm-run-omn14534"
_SUBTASK_ID = "subtask-3"


def _call_request() -> ModelLlmDelegationCallRequest:
    return ModelLlmDelegationCallRequest(
        request_id=f"{_RUN_ID}-{_SUBTASK_ID}",
        correlation_id=f"{_RUN_ID}-{_SUBTASK_ID}",
        causation_id=_RUN_ID,
        model_id="Qwen/Qwen3-Coder-480B-A35B-Instruct",
        endpoint_ref="LOCAL_QWEN_ENDPOINT",
        prompt="do the thing",
        prompt_hash="sha256:deadbeef",
        task_id=_SUBTASK_ID,
        timeout_seconds=30.0,
    )


def _completed_event() -> ModelLlmDelegationCompletedEvent:
    return ModelLlmDelegationCompletedEvent(
        correlation_id=f"{_RUN_ID}-{_SUBTASK_ID}",
        causation_id=_RUN_ID,
        request_id=f"{_RUN_ID}-{_SUBTASK_ID}",
        task_type="generic",
        task_id=_SUBTASK_ID,
        selected_model="Qwen/Qwen3-Coder-480B-A35B-Instruct",
        model_id="Qwen/Qwen3-Coder-480B-A35B-Instruct",
        model_tier="local",
        provider="mlx",
        endpoint_ref="LOCAL_QWEN_ENDPOINT",
        tokens_in=100,
        tokens_out=50,
        latency_ms=842,
        actual_cost_usd=Decimal("0.00"),
        opus_equivalent_cost_usd=Decimal("0.03"),
        savings_usd=Decimal("0.03"),
        usage_source=EnumUsageSource.MEASURED,
        cost_basis=EnumCostBasis.ZERO_MARGINAL_API_COST,
        pricing_manifest_version="v1",
        pricing_manifest_hash="sha256:pricing",
        output_hash="sha256:output",
        prompt_hash="sha256:deadbeef",
        routing_policy_hash="sha256:policy",
        policy_hash="sha256:policy",
        registry_hash="sha256:registry",
        success=True,
        quality_score=0.95,
        escalated_to=None,
        escalation_reason=None,
        redacted_summary=None,
        created_at=datetime(2026, 7, 12, tzinfo=UTC),
    )


def _escalation_event() -> ModelLlmDelegationEscalationTriggeredEvent:
    return ModelLlmDelegationEscalationTriggeredEvent(
        correlation_id=f"{_RUN_ID}-{_SUBTASK_ID}",
        causation_id=_RUN_ID,
        request_id=f"{_RUN_ID}-{_SUBTASK_ID}",
        task_type="generic",
        task_id=_SUBTASK_ID,
        model_id="Qwen/Qwen3-Coder-480B-A35B-Instruct",
        attempt_number=1,
        failure_class=EnumDelegationFailureClass.QUALITY_GATE_FAILED,
        escalation_reason="quality gate rejected output",
        next_model_id="claude-opus-4-8",
        created_at=datetime(2026, 7, 12, tzinfo=UTC),
    )


def _all_tiers_failed_event() -> ModelLlmDelegationAllTiersFailedEvent:
    return ModelLlmDelegationAllTiersFailedEvent(
        correlation_id=f"{_RUN_ID}-{_SUBTASK_ID}",
        causation_id=_RUN_ID,
        request_id=f"{_RUN_ID}-{_SUBTASK_ID}",
        task_type="generic",
        task_id=_SUBTASK_ID,
        attempted_models=("local-tier", "cloud-tier"),
        failure_classes=(
            EnumDelegationFailureClass.RATE_LIMITED,
            EnumDelegationFailureClass.PROVIDER_AUTH_FAILED,
        ),
        created_at=datetime(2026, 7, 12, tzinfo=UTC),
    )


def _fanout_completed() -> ModelSwarmFanoutResult:
    return ModelSwarmFanoutResult(
        dispatches=(),
        wall_latency_ms=5000,
        sum_subtask_latency_ms=12000,
        run_id=_RUN_ID,
        completed_count=4,
        failed_count=1,
    )


# ---------------------------------------------------------------------------
# RED: the internal ModelDelegationEvent shape never matches a real producer.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "real_event",
    [
        _call_request(),
        _completed_event(),
        _escalation_event(),
        _all_tiers_failed_event(),
    ],
)
def test_real_producer_dump_does_not_validate_as_internal_model(
    real_event: object,
) -> None:
    """RED: proves ModelDelegationEvent is not the real wire shape.

    If this ever starts passing, the real producer models grew run_id/
    subtask_id/event_type fields and the adapter layer in
    handler_swarm_subtask_state.py may be simplifiable/obsolete — it is NOT
    a signal that the adapters are unnecessary today.
    """
    dump = real_event.model_dump(mode="json")  # type: ignore[attr-defined]
    with pytest.raises(ValidationError):
        ModelDelegationEvent(**dump)


# ---------------------------------------------------------------------------
# GREEN: handle() adapts each real producer model and reduces correctly.
# ---------------------------------------------------------------------------


def test_call_request_dispatches_to_assigned() -> None:
    handler = HandlerSwarmSubtaskState()
    result = handler.handle(_call_request())
    subtask = result["new_state"]["subtasks"][_SUBTASK_ID]
    assert subtask["state"] == EnumSubtaskState.ASSIGNED.value
    assert subtask["run_id"] == _RUN_ID
    assert subtask["model_id"] == "Qwen/Qwen3-Coder-480B-A35B-Instruct"


def test_completed_event_dispatches_to_completed() -> None:
    handler = HandlerSwarmSubtaskState()
    result = handler.handle(_completed_event())
    subtask = result["new_state"]["subtasks"][_SUBTASK_ID]
    assert subtask["state"] == EnumSubtaskState.COMPLETED.value
    assert subtask["latency_ms"] == 842
    assert subtask["terminal_event_id"] == f"{_RUN_ID}-{_SUBTASK_ID}"


def test_escalation_event_dispatches_to_escalating() -> None:
    handler = HandlerSwarmSubtaskState()
    result = handler.handle(_escalation_event())
    subtask = result["new_state"]["subtasks"][_SUBTASK_ID]
    assert subtask["state"] == EnumSubtaskState.ESCALATING.value
    assert (
        subtask["failure_class"] == EnumDelegationFailureClass.QUALITY_GATE_FAILED.value
    )


def test_all_tiers_failed_event_dispatches_to_failed() -> None:
    handler = HandlerSwarmSubtaskState()
    result = handler.handle(_all_tiers_failed_event())
    subtask = result["new_state"]["subtasks"][_SUBTASK_ID]
    assert subtask["state"] == EnumSubtaskState.FAILED.value
    # Last attempted model's failure class is attributed to the terminal event.
    assert (
        subtask["failure_class"]
        == EnumDelegationFailureClass.PROVIDER_AUTH_FAILED.value
    )


def test_fanout_completed_is_noop() -> None:
    handler = HandlerSwarmSubtaskState()
    result = handler.handle(_fanout_completed())
    assert result["state_changed"] is False
    assert result["new_state"]["subtasks"] == {}


def test_unrecognized_payload_type_raises_typeerror() -> None:
    handler = HandlerSwarmSubtaskState()
    with pytest.raises(TypeError):
        handler.handle(object())


def test_each_dispatch_reduces_against_a_fresh_state() -> None:
    """Documents a known limitation, not silently hides it.

    handle() has no runtime-injected current_state (this reducer is not
    bound to OMN-14208 state_io — see contract.yaml). Two independent
    dispatches for the SAME subtask each reduce against an empty run state
    rather than accumulating. OMN-14534 fixes the crash-and-drop bug (every
    dispatch previously failed); it does not add cross-message FSM
    continuity — that is a separate, tracked follow-up.
    """
    handler = HandlerSwarmSubtaskState()

    assign_result = handler.handle(_call_request())
    assert (
        assign_result["new_state"]["subtasks"][_SUBTASK_ID]["state"]
        == EnumSubtaskState.ASSIGNED.value
    )
    assert assign_result["new_state"]["subtasks"][_SUBTASK_ID]["attempt_count"] == 1

    complete_result = handler.handle(_completed_event())
    # attempt_count restarts at 0 (not 2) because no prior state was carried
    # in — the observable signature of the missing state_io binding.
    assert complete_result["new_state"]["subtasks"][_SUBTASK_ID]["attempt_count"] == 0
