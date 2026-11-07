# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A replayed delegation request or quality-gate result past its deadline is expired (OMN-22822).

The dead-letter replay node re-delivered inference responses from 2026-11-03 to
2026-11-05 onto the live topic on 2026-11-07, and the orchestrator took them
for answers. The orchestrator handles two more replayable inputs the same way:

* a delegation request. The recorded request of run ``c490e90f`` was emitted
  2026-11-04T20:14Z. A replay of it at 2026-11-07T02:40Z used to open a routing
  intent (a new run, or for a run still ``RECEIVED`` a replayed routing intent)
  and so a new provider attempt, 54 hours after the caller stopped waiting;
* a quality-gate result. The recorded run row of ``c490e90f`` advanced to
  ``INFERENCE_COMPLETED`` plus a replayed failing verdict used to send the run
  to the next rung, which calls a provider again.

The deadline of both is the contract-declared completion bound
(``completion_bound.max_wall_seconds``, 900 s) after the run's start: the
request's own ``emitted_at`` for a request, the run's start epoch for a gate
result (which carries no time of its own). The bound is read from the node
contract or injected, never written into this handler.

The fixtures are the recorded run row (shared with OMN-22478), the recorded
request inside it, and replay envelopes shaped like the recorded replay of the
inference response (see each fixture's ``provenance_note``).
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from omnibase_core.enums.enum_delegation_operational_outcome import (
    EnumDelegationOperationalOutcome,
)
from omnibase_core.models.delegation.wire.model_delegation_failed import (
    ModelDelegationFailed,
)
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.enums import EnumDispatchStatus
from pydantic import BaseModel

from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_delegation_request import (
    DispatcherDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_quality_gate_result import (
    DispatcherQualityGateResult,
)
from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    DelegationWorkflowState,
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_intent import (
    ModelInferenceIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_quality_gate_intent import (
    ModelQualityGateIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_routing_intent import (
    ModelRoutingIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.state_codec import decode
from omnimarket.nodes.node_delegation_quality_gate_reducer.models.model_quality_gate_result import (
    ModelQualityGateResult,
)

pytestmark = pytest.mark.usefixtures("stub_provider_quota_reader")

_FIXTURES = Path(__file__).parent / "fixtures"
_ROW = _FIXTURES / "omn22478_abandoned_rows" / "c490e90f_superseded_rejections.json"
_REQUEST_REPLAY = (
    _FIXTURES / "omn22822_replayed_requests" / "c490e90f_replayed_request.json"
)
_GATE_REPLAY = (
    _FIXTURES / "omn22822_replayed_gate_results" / "c490e90f_replayed_gate_result.json"
)
_CID = UUID("c490e90f-bdbc-4bfd-9fb9-3eeadf485146")
_NEVER_EXPIRES = 10**9
_NEW_ATTEMPT_TYPES = (ModelRoutingIntent, ModelInferenceIntent, ModelQualityGateIntent)


def _load(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_text())
    return loaded


def _recorded_request() -> ModelDelegationRequest:
    record = _load(_REQUEST_REPLAY)
    assert record["headers"]["x-replayed-by"] == "node_dlq_replay_effect"
    return ModelDelegationRequest.model_validate(record["payload"])


def _recorded_gate_replay() -> ModelQualityGateResult:
    record = _load(_GATE_REPLAY)
    assert record["headers"]["x-replayed-by"] == "node_dlq_replay_effect"
    return ModelQualityGateResult.model_validate(record["payload"])


def _recorded_workflow() -> DelegationWorkflowState:
    return decode(json.dumps(_load(_ROW)["payload"]))


def _gate_ready_workflow() -> DelegationWorkflowState:
    """The recorded run advanced to the state a gate result is waiting for."""
    workflow = _recorded_workflow()
    workflow.customer_route_retry_count = 0
    workflow.state = EnumDelegationState.INFERENCE_COMPLETED
    workflow.inference_intent_in_flight = False
    workflow.inference_content = "A short answer."
    workflow.inference_model_used = "claude-sonnet-5-5"
    return workflow


def _fresh(request: ModelDelegationRequest) -> ModelDelegationRequest:
    return request.model_copy(update={"emitted_at": datetime.now(UTC)})


def _received_workflow(request: ModelDelegationRequest) -> DelegationWorkflowState:
    return DelegationWorkflowState(
        correlation_id=request.correlation_id, request=request
    )


def _terminals(events: list[BaseModel]) -> list[ModelDelegationFailed]:
    return [e for e in events if isinstance(e, ModelDelegationFailed)]


def _shape(events: list[BaseModel]) -> list[str]:
    return [type(e).__name__ for e in events]


# ---------------------------------------------------------------- the pairs


@pytest.mark.unit
def test_recorded_request_replay_is_past_the_deadline() -> None:
    request = _recorded_request()
    replayed_at = _load(_REQUEST_REPLAY)["timestamp_ms"] / 1000
    assert request.correlation_id == _CID
    assert replayed_at - request.emitted_at.timestamp() > 900


@pytest.mark.unit
def test_recorded_gate_replay_is_for_a_run_past_its_bound() -> None:
    workflow = _gate_ready_workflow()
    replay = _recorded_gate_replay()
    assert replay.correlation_id == workflow.correlation_id
    assert replay.passed is False
    replayed_at_ns = _load(_GATE_REPLAY)["timestamp_ms"] * 1_000_000
    assert (replayed_at_ns - workflow.started_at_ns) // 1_000_000_000 > 900


# ------------------------------------------------------- delegation request


@pytest.mark.unit
def test_replayed_request_past_deadline_is_expired_and_not_routed() -> None:
    handler = HandlerDelegationWorkflow(workflows={})
    events = handler.handle_delegation_request(_recorded_request())

    assert not [e for e in events if isinstance(e, _NEW_ATTEMPT_TYPES)]
    workflow = handler._workflows[_CID]
    assert workflow.state == EnumDelegationState.FAILED
    expired = workflow.expired_delegation_request
    assert expired is not None
    assert expired.correlation_id == _CID
    assert expired.completion_bound_seconds == 900
    assert expired.elapsed_seconds > 900
    assert expired.emitted_at == _recorded_request().emitted_at
    terminals = _terminals(events)
    assert len(terminals) == 1
    assert (
        terminals[0].operational_outcome
        == EnumDelegationOperationalOutcome.BOUNDARY_FAILURE
    )
    assert "recorded as expired and not routed" in terminals[0].failure_reason


@pytest.mark.unit
def test_replayed_request_for_a_received_run_past_deadline_replays_no_routing_intent() -> (
    None
):
    request = _recorded_request()
    workflow = _received_workflow(request)
    handler = HandlerDelegationWorkflow(workflows={_CID: workflow})
    events = handler.handle_delegation_request(request)

    assert not [e for e in events if isinstance(e, _NEW_ATTEMPT_TYPES)]
    assert workflow.state == EnumDelegationState.FAILED
    assert workflow.expired_delegation_request is not None
    assert workflow.routing_intent_replayed is False
    assert len(_terminals(events)) == 1


@pytest.mark.unit
def test_a_second_delivery_of_the_expired_request_changes_nothing() -> None:
    handler = HandlerDelegationWorkflow(workflows={})
    handler.handle_delegation_request(_recorded_request())
    assert handler.handle_delegation_request(_recorded_request()) == []
    assert handler._workflows[_CID].state == EnumDelegationState.FAILED


@pytest.mark.unit
def test_request_inside_the_deadline_behaves_as_before() -> None:
    request = _fresh(_recorded_request())
    handler = HandlerDelegationWorkflow(workflows={})
    events = handler.handle_delegation_request(request)

    assert [type(e) for e in events] == [ModelRoutingIntent]
    workflow = handler._workflows[_CID]
    assert workflow.state == EnumDelegationState.RECEIVED
    assert getattr(workflow, "expired_delegation_request", None) is None


@pytest.mark.unit
def test_received_run_replayed_inside_the_deadline_still_gets_its_routing_intent() -> (
    None
):
    request = _fresh(_recorded_request())
    workflow = _received_workflow(request)
    handler = HandlerDelegationWorkflow(workflows={_CID: workflow})
    events = handler.handle_delegation_request(request)

    assert [type(e) for e in events] == [ModelRoutingIntent]
    assert workflow.routing_intent_replayed is True
    assert getattr(workflow, "expired_delegation_request", None) is None


@pytest.mark.unit
def test_request_bound_is_injected_not_hard_coded() -> None:
    old = _recorded_request()
    unbounded = HandlerDelegationWorkflow(
        workflows={}, completion_bound_seconds=_NEVER_EXPIRES
    )
    assert [type(e) for e in unbounded.handle_delegation_request(old)] == [
        ModelRoutingIntent
    ]

    # A request 100 s old is inside 900 s and past a 60 s bound.
    young = old.model_copy(
        update={"emitted_at": datetime.fromtimestamp(time.time() - 100, UTC)}
    )
    inside = HandlerDelegationWorkflow(workflows={})
    assert [type(e) for e in inside.handle_delegation_request(young)] == [
        ModelRoutingIntent
    ]
    past = HandlerDelegationWorkflow(workflows={}, completion_bound_seconds=60)
    expired = past.handle_delegation_request(young)
    assert len(_terminals(expired)) == 1
    assert past._workflows[_CID].expired_delegation_request is not None
    assert (
        past._workflows[_CID].expired_delegation_request.completion_bound_seconds == 60
    )  # type: ignore[union-attr]


# ------------------------------------------------------- quality-gate result


@pytest.mark.unit
def test_replayed_gate_result_past_deadline_is_expired_and_not_reevaluated() -> None:
    workflow = _gate_ready_workflow()
    handler = HandlerDelegationWorkflow(workflows={_CID: workflow})
    events = handler.handle_gate_result(_recorded_gate_replay())

    assert not [e for e in events if isinstance(e, _NEW_ATTEMPT_TYPES)]
    assert workflow.state == EnumDelegationState.FAILED
    assert workflow.gate_result is None
    assert workflow.escalation_count == 0
    expired = workflow.expired_quality_gate_result
    assert expired is not None
    assert expired.correlation_id == _CID
    assert expired.result_passed is False
    assert expired.completion_bound_seconds == 900
    assert expired.elapsed_seconds > 900
    terminals = _terminals(events)
    assert len(terminals) == 1
    assert terminals[0].operational_outcome == EnumDelegationOperationalOutcome.TIMEOUT
    assert "recorded as expired and not re-evaluated" in terminals[0].failure_reason


@pytest.mark.unit
def test_a_second_delivery_of_the_expired_gate_result_changes_nothing() -> None:
    workflow = _gate_ready_workflow()
    handler = HandlerDelegationWorkflow(workflows={_CID: workflow})
    handler.handle_gate_result(_recorded_gate_replay())
    assert handler.handle_gate_result(_recorded_gate_replay()) == []
    assert workflow.state == EnumDelegationState.FAILED


@pytest.mark.unit
def test_gate_result_inside_the_bound_behaves_as_before() -> None:
    inside = _gate_ready_workflow()
    inside.started_at_ns = time.time_ns()
    events = HandlerDelegationWorkflow(workflows={_CID: inside}).handle_gate_result(
        _recorded_gate_replay()
    )

    # The same verdict on the same run, judged by a handler whose bound can never
    # pass, is what the code did before the deadline existed.
    before = _gate_ready_workflow()
    reference = HandlerDelegationWorkflow(
        workflows={_CID: before}, completion_bound_seconds=_NEVER_EXPIRES
    ).handle_gate_result(_recorded_gate_replay())

    assert getattr(inside, "expired_quality_gate_result", None) is None
    assert getattr(before, "expired_quality_gate_result", None) is None
    assert _shape(events) == _shape(reference)
    assert inside.state == before.state
    assert inside.escalation_count == before.escalation_count
    assert inside.gate_result == before.gate_result


@pytest.mark.unit
def test_gate_bound_is_injected_not_hard_coded() -> None:
    young = _gate_ready_workflow()
    young.started_at_ns = time.time_ns() - 100 * 1_000_000_000
    handler = HandlerDelegationWorkflow(
        workflows={_CID: young}, completion_bound_seconds=60
    )
    events = handler.handle_gate_result(_recorded_gate_replay())
    assert len(_terminals(events)) == 1
    assert young.expired_quality_gate_result is not None
    assert young.expired_quality_gate_result.completion_bound_seconds == 60


# ------------------------------------------------- golden and error chains


def _envelope(payload: object, correlation_id: UUID) -> ModelEventEnvelope[object]:
    return ModelEventEnvelope(
        envelope_id=uuid4(),
        payload=payload,
        correlation_id=correlation_id,
        envelope_timestamp=datetime.now(UTC),
    )


@pytest.mark.unit
async def test_golden_chain_replayed_request_through_the_dispatcher() -> None:
    handler = HandlerDelegationWorkflow(workflows={})
    dispatcher = DispatcherDelegationRequest(handler)
    record = _load(_REQUEST_REPLAY)

    result = await dispatcher.handle(_envelope(record["payload"], _CID))

    assert result.status == EnumDispatchStatus.SUCCESS
    assert _shape(list(result.output_events)) == ["ModelDelegationFailed"]
    failed = result.output_events[0]
    assert isinstance(failed, ModelDelegationFailed)
    assert failed.correlation_id == _CID
    assert (
        failed.operational_outcome == EnumDelegationOperationalOutcome.BOUNDARY_FAILURE
    )


@pytest.mark.unit
async def test_golden_chain_request_inside_the_deadline_still_routes() -> None:
    handler = HandlerDelegationWorkflow(workflows={})
    dispatcher = DispatcherDelegationRequest(handler)
    payload = _fresh(_recorded_request()).model_dump(mode="json")

    result = await dispatcher.handle(_envelope(payload, _CID))

    assert result.status == EnumDispatchStatus.SUCCESS
    assert _shape(list(result.output_events)) == ["ModelRoutingIntent"]


@pytest.mark.unit
async def test_error_chain_request_redelivery_and_malformed_replay() -> None:
    handler = HandlerDelegationWorkflow(workflows={})
    dispatcher = DispatcherDelegationRequest(handler)
    payload = _load(_REQUEST_REPLAY)["payload"]

    first = await dispatcher.handle(_envelope(payload, _CID))
    again = await dispatcher.handle(_envelope(payload, _CID))
    malformed = await dispatcher.handle(_envelope({"correlation_id": str(_CID)}, _CID))

    assert _shape(list(first.output_events)) == ["ModelDelegationFailed"]
    assert again.status == EnumDispatchStatus.SUCCESS
    assert list(again.output_events) == []
    assert malformed.status == EnumDispatchStatus.INVALID_MESSAGE
    assert list(malformed.output_events) == []
    assert handler._workflows[_CID].state == EnumDelegationState.FAILED


@pytest.mark.unit
async def test_golden_chain_replayed_gate_result_through_the_dispatcher() -> None:
    workflow = _gate_ready_workflow()
    handler = HandlerDelegationWorkflow(workflows={_CID: workflow})
    dispatcher = DispatcherQualityGateResult(handler)
    record = _load(_GATE_REPLAY)

    result = await dispatcher.handle(_envelope(record["payload"], _CID))

    assert result.status == EnumDispatchStatus.SUCCESS
    terminals = [
        e for e in result.output_events if isinstance(e, ModelDelegationFailed)
    ]
    assert len(terminals) == 1
    assert terminals[0].operational_outcome == EnumDelegationOperationalOutcome.TIMEOUT
    assert not [e for e in result.output_events if isinstance(e, _NEW_ATTEMPT_TYPES)]
    assert workflow.expired_quality_gate_result is not None


@pytest.mark.unit
async def test_error_chain_gate_redelivery_and_unknown_run() -> None:
    workflow = _gate_ready_workflow()
    handler = HandlerDelegationWorkflow(workflows={_CID: workflow})
    dispatcher = DispatcherQualityGateResult(handler)
    payload = _load(_GATE_REPLAY)["payload"]

    await dispatcher.handle(_envelope(payload, _CID))
    again = await dispatcher.handle(_envelope(payload, _CID))
    other = uuid4()
    unknown = await dispatcher.handle(
        _envelope({**payload, "correlation_id": str(other)}, other)
    )

    assert again.status == EnumDispatchStatus.SUCCESS
    assert list(again.output_events) == []
    assert unknown.status == EnumDispatchStatus.SUCCESS
    assert list(unknown.output_events) == []
    assert workflow.state == EnumDelegationState.FAILED
