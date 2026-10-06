# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Five faults through the bus-path delegation workflow (OMN-19450).

The bus path is the one the deployed runtime serves. The consumer-facing
``HandlerDelegateSkill`` publishes a delegation request through the real
``RuntimeDelegationDispatchPort`` onto an in-memory event bus; the orchestrator
stand-in on the other side of that bus is the real ``HandlerDelegationWorkflow``,
which emits an inference intent, the real inference effect
(``HandlerInferenceIntent``) makes a real HTTP call, and the real quality-gate
reducer judges what came back. The workflow's terminal travels back over the bus
and the port hands it to the handler, which builds the terminal the caller reads.
Only the provider host is redirected, to a loopback socket that answers real HTTP,
so the effect's transport, status classification and truncation refusal all run as
they do in production.

Two terminals are asserted for every fault because two readers depend on them:
the canonical workflow terminal is what the ``delegation_events`` projection
copies its cause from, and the consumer-facing terminal is what the caller sees.

Each fault must end in a terminal whose ``terminal_failure_cause`` names what
decided the run, and none may read ``provider_error`` or state no cause:

* wrong key, HTTP 401        -> ``auth_failed``
* HTTP 429 with a quota body -> ``provider_quota_exhausted``
* a call past its timeout    -> ``timeout``
* a gate veto                -> ``quality_gate_refused``, naming the vetoing rule
* ``finish_reason=length``   -> ``quality_gate_refused``, carrying the stop
  reason and the truncated flag on the deciding attempt

The veto and the truncation share a cause because the gate decided both runs.
They are told apart by typed evidence on the deciding attempt, never by the
cause.

The one seam this file stubs, beside the bus stand-in, is the escalation decision: each run is held to one
rung so the fault under test is the event that decided it. The routing and
sibling-retry decisions are covered by their own tests.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
from omnibase_core.enums.enum_delegation_terminal_failure_cause import (
    EnumDelegationTerminalFailureCause,
)
from omnibase_core.models.delegation.wire import (
    ModelInferenceIntent,
    ModelQualityGateIntent,
)
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.enums.enum_delegation_acceptance import EnumDelegationAcceptanceReason
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillResponse,
)
from omnimarket.models.delegation.wire.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    RuntimeDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
    TOPIC_ID_DELEGATION_COMPLETED,
    TOPIC_ID_DELEGATION_FAILED,
    TOPIC_ID_DELEGATION_REQUEST,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    DelegationWorkflowState,
    HandlerDelegationWorkflow,
    _inference_error_failure_class,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationFailed,
)
from omnimarket.nodes.node_delegation_quality_gate_reducer.handlers.handler_quality_gate_intent import (
    HandlerQualityGateIntent,
)
from omnimarket.nodes.node_llm_delegation_call_effect.handlers.handler_inference_intent import (
    HandlerInferenceIntent,
)
from omnimarket.routing.model_escalation_decision_result import (
    ModelEscalationDecisionResult,
)
from tests.chains.local.harness import LocalProviderStub

pytestmark = [
    pytest.mark.unit,
    pytest.mark.local_chain,
    pytest.mark.usefixtures("stub_provider_quota_reader"),
]

_MODEL = "omn19450-fault-model"
_TASK_TYPE = "document"
_PROMPT = "write a short paragraph describing what a semantic version is"
_ANSWER = (
    "### ANSWER\nA semantic version has three numeric parts, major, minor and "
    "patch, and each part is raised for a different kind of change."
)
_VETO_RULE = "no_refusal"
_TRUNCATION_CHECK = "not_truncated_by_output_budget"
_FAULTS = ("wrong_key", "quota", "timeout", "gate_veto", "truncation")
_EXPECTED_CAUSES = {
    "wrong_key": EnumDelegationTerminalFailureCause.AUTH_FAILED,
    "quota": EnumDelegationTerminalFailureCause.PROVIDER_QUOTA_EXHAUSTED,
    "timeout": EnumDelegationTerminalFailureCause.TIMEOUT,
    "gate_veto": EnumDelegationTerminalFailureCause.QUALITY_GATE_REFUSED,
    "truncation": EnumDelegationTerminalFailureCause.QUALITY_GATE_REFUSED,
}


def _no_further_rung(
    workflow: DelegationWorkflowState, **_kwargs: object
) -> ModelEscalationDecisionResult:
    del workflow
    return ModelEscalationDecisionResult(
        can_escalate=False, terminal_failure_reason="fault_suite_single_rung"
    )


def _stub_for(fault: str) -> LocalProviderStub:
    stub = LocalProviderStub(model_id=_MODEL, content=_ANSWER)
    if fault == "wrong_key":
        stub.completion_status = 401
    elif fault == "quota":
        stub.completion_status = 429
        stub.completion_error_body = {
            "error": {
                "message": "rate limit exceeded: quota exceeded",
                "type": "insufficient_quota",
                "code": 429,
            }
        }
    elif fault == "timeout":
        stub.completion_delay_seconds = 5.0
    elif fault == "gate_veto":
        stub.content = "### ANSWER\nI cannot help with that request."
    elif fault == "truncation":
        stub.finish_reason = "length"
    return stub


def _routing_decision(correlation_id: UUID, endpoint_url: str) -> ModelRoutingDecision:
    return ModelRoutingDecision(
        correlation_id=correlation_id,
        task_type=_TASK_TYPE,
        selected_model=_MODEL,
        selected_backend_id=uuid5(NAMESPACE_DNS, "omninode.ai/backends/omn19450"),
        endpoint_url=endpoint_url,
        cost_tier="low",
        max_context_tokens=65536,
        timeout_ms=1500,
        max_tokens=4096,
        system_prompt="You are a documentation assistant.",
        rationale="OMN-19450 fault suite: one rung, one provider.",
        dod_deterministic=("response_non_empty",),
        dod_heuristic=("no_refusal",),
        tier_name="local",
    )


@dataclass(frozen=True)
class _BusRun:
    """What one fault produced, as each of the two readers sees it."""

    canonical: ModelDelegationFailed
    consumer: ModelDelegateSkillResponse


def _orchestrate(
    request: ModelDelegationRequest,
    endpoint_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> ModelDelegationFailed:
    """The orchestrator side of the bus: request -> route -> infer -> gate -> terminal."""
    workflow = HandlerDelegationWorkflow()
    monkeypatch.setattr(
        workflow, "_maybe_retry_sibling_backend", lambda *_a, **_k: None
    )
    monkeypatch.setattr(workflow, "_maybe_retry_customer_route", lambda *_a, **_k: None)
    monkeypatch.setattr(workflow, "_maybe_retry_local", lambda *_a, **_k: None)
    monkeypatch.setattr(workflow, "_decide_escalation", _no_further_rung)

    workflow.handle_delegation_request(request)
    intents = workflow.handle_routing_decision(
        _routing_decision(request.correlation_id, endpoint_url)
    )
    inference_intent = next(i for i in intents if isinstance(i, ModelInferenceIntent))

    response = HandlerInferenceIntent().handle(inference_intent)
    events = workflow.handle_inference_response(response)

    gate_intents = [e for e in events if isinstance(e, ModelQualityGateIntent)]
    if gate_intents:
        gate_result = HandlerQualityGateIntent().handle(gate_intents[0])
        events = workflow.handle_gate_result(gate_result)

    failed = [e for e in events if isinstance(e, ModelDelegationFailed)]
    assert len(failed) == 1, events
    return failed[0]


async def _run_fault(
    fault: str,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> _BusRun:
    """Drive one fault through the consumer handler, the bus and the orchestrator."""
    stub = _stub_for(fault)
    stub.start()
    bus = EventBusInmemory(environment="test", group="omn19450-fault-suite")
    canonical: list[ModelDelegationFailed] = []
    await bus.start()

    async def orchestrator_stand_in(message: ModelEventMessage) -> None:
        envelope = ModelEventEnvelope[ModelDelegationRequest].model_validate_json(
            message.value
        )
        terminal = _orchestrate(envelope.payload, stub.completions_url, monkeypatch)
        canonical.append(terminal)
        # The runtime publishes one canonical envelope whose payload is the
        # unwrapped terminal (DispatchResultApplier's wire shape).
        reply = ModelEventEnvelope[ModelDelegationFailed](
            payload=terminal,
            correlation_id=terminal.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=TOPIC_ID_DELEGATION_FAILED,
            source_tool="omn19450-fault-suite",
        )
        await bus.publish(
            TOPIC_ID_DELEGATION_FAILED,
            None,
            reply.model_dump_json().encode("utf-8"),
            None,
        )

    try:
        await bus.subscribe(
            TOPIC_ID_DELEGATION_REQUEST,
            group_id=f"omn19450-orchestrator-{uuid4().hex}",
            on_message=orchestrator_stand_in,
        )
        handler = HandlerDelegateSkill(
            dispatch_port=RuntimeDelegationDispatchPort(
                event_bus=bus,
                completed_topic=TOPIC_ID_DELEGATION_COMPLETED,
                failed_topic=TOPIC_ID_DELEGATION_FAILED,
            )
        )
        consumer = await handler.handle(
            ModelDelegateSkillRequest(
                prompt=_PROMPT,
                task_type=_TASK_TYPE,
                source="external-client",
                correlation_id=uuid4(),
            )
        )
    finally:
        await bus.close()
        stub.stop()

    assert len(canonical) == 1, (fault, canonical)
    record_property(
        f"bus_fault_{fault}_canonical",
        json.dumps(canonical[0].model_dump(mode="json"), sort_keys=True, default=str),
    )
    record_property(
        f"bus_fault_{fault}_consumer",
        json.dumps(consumer.model_dump(mode="json"), sort_keys=True, default=str),
    )
    assert isinstance(consumer, ModelDelegateSkillResponse)
    return _BusRun(canonical=canonical[0], consumer=consumer)


@pytest.mark.parametrize("fault", _FAULTS)
async def test_each_fault_decides_its_own_cause_and_none_reads_provider_error(
    fault: str,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    run = await _run_fault(fault, monkeypatch, record_property)

    for reader, cause in (
        ("consumer terminal", run.consumer.terminal_failure_cause),
        ("canonical terminal", run.canonical.terminal_failure_cause),
    ):
        assert cause is not EnumDelegationTerminalFailureCause.PROVIDER_ERROR, (
            fault,
            reader,
        )
        assert cause is _EXPECTED_CAUSES[fault], (fault, reader, cause)


async def test_the_veto_names_its_rule_on_the_deciding_attempt(
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    run = await _run_fault("gate_veto", monkeypatch, record_property)
    deciding = run.consumer.attempts[-1]

    assert deciding.acceptance_reason in {
        EnumDelegationAcceptanceReason.HEURISTIC_VETO,
        EnumDelegationAcceptanceReason.DETERMINISTIC_FLOOR_FAILED,
        EnumDelegationAcceptanceReason.ACCEPTANCE_CRITERIA_FAILED,
    }, deciding
    assert any(
        reason.startswith("REFUSAL") for reason in deciding.error_message.split("; ")
    ), deciding
    # The vetoing rule is named on the terminal's own reason, by name.
    assert f"deciding_rules={_VETO_RULE}" in run.canonical.failure_reason, (
        run.canonical.failure_reason
    )


async def test_the_truncation_carries_the_stop_reason_and_the_flag(
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    run = await _run_fault("truncation", monkeypatch, record_property)
    deciding = dict(run.canonical.escalation_history[-1])

    assert deciding["finish_reason"] == "length", deciding
    assert deciding["truncated"] is True, deciding


async def test_the_veto_and_the_truncation_are_told_apart_by_their_deciding_attempt(
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    veto = dict(
        (
            await _run_fault("gate_veto", monkeypatch, record_property)
        ).canonical.escalation_history[-1]
    )
    truncation = dict(
        (
            await _run_fault("truncation", monkeypatch, record_property)
        ).canonical.escalation_history[-1]
    )

    assert veto.get("finish_reason") != truncation.get("finish_reason")
    assert bool(veto.get("truncated")) is False
    assert truncation.get("truncated") is True


@pytest.mark.parametrize("status", [401, 403])
def test_a_rejected_credential_is_not_read_as_a_rate_limit_by_a_digit_in_the_url(
    status: int,
) -> None:
    """The message carries the call's URL, and a UUID or port can contain ``429``."""
    message = (
        f"provider HTTP {status} Unauthorized for "
        "http://127.0.0.1:34290/v1/chat/completions"
        "?onex_cid=0f429a1c-5d3e-4b7a-9c2e-8a1b429c0d4e; "
        'response_body={"error": {"message": "Incorrect API key provided."}}'
    )

    assert (
        _inference_error_failure_class(message)
        is EnumDelegationFailureClass.PROVIDER_AUTH_FAILED
    )
