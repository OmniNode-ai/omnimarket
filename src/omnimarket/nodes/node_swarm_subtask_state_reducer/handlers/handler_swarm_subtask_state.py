# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Swarm subtask state reducer handler.

Pure reducer: delta(state, event) -> (new_state, projection_freshness).

Idempotency key: run_id + subtask_id + terminal_event_id.
Ordering authority: topic/partition/offset, not emitted_at timestamps.

OMN-14534: handle() is the runtime dispatch entrypoint. It receives one of 5
real producer wire models (see the ``_from_*`` adapters below) — never the
internal ``ModelDelegationEvent``, which no producer ever emits — and adapts
each into ``ModelDelegationEvent`` before delegating to the pure ``delta()``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import yaml

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
from omnimarket.nodes.node_swarm_subtask_state_reducer.models.model_projection_freshness import (
    EnumFreshnessState,
    ModelProjectionFreshness,
)
from omnimarket.nodes.node_swarm_subtask_state_reducer.models.model_subtask_state import (
    TERMINAL_STATES,
    EnumSubtaskState,
    ModelSubtaskState,
    ModelSwarmRunState,
)
from omnimarket.nodes.node_swarm_subtask_state_reducer.models.model_swarm_subtask_input import (
    EnumDelegationEventType,
    ModelDelegationEvent,
    ModelSwarmSubtaskReducerInput,
)
from omnimarket.nodes.node_swarm_subtask_state_reducer.models.model_swarm_subtask_output import (
    ModelSwarmSubtaskReducerOutput,
)

logger = logging.getLogger(__name__)

REDUCER_VERSION = "1.0.0"

HandlerType = Literal["NODE_HANDLER"]
HandlerCategory = Literal["COMPUTE"]

_EVENT_TO_STATE: dict[EnumDelegationEventType, EnumSubtaskState] = {
    EnumDelegationEventType.DELEGATION_EXECUTE: EnumSubtaskState.ASSIGNED,
    EnumDelegationEventType.DELEGATION_CALL_COMPLETED: EnumSubtaskState.COMPLETED,
    EnumDelegationEventType.DELEGATION_ESCALATION_TRIGGERED: EnumSubtaskState.ESCALATING,
    EnumDelegationEventType.DELEGATION_ALL_TIERS_FAILED: EnumSubtaskState.FAILED,
}

# OMN-14534: node_swarm_fanout_orchestrator (the exclusive publisher of
# onex.cmd.omnimarket.delegation-execute.v1 — verified, no other producer)
# stamps causation_id=run_id and task_id=subtask_id on the outbound command
# (handler_swarm_fanout.py: `"causation_id": request.run_id`,
# `"task_id": subtask.subtask_id`). node_llm_delegation_call_effect's handler
# copies both fields verbatim onto every one of its 3 output event types, so
# the mapping holds for every message this reducer receives on any of its 5
# subscribed topics.
#
# source_partition/source_offset stay at the ModelDelegationEvent default (0):
# the def-B typed-payload dispatch contract hands handle() the validated
# domain payload only, never Kafka delivery metadata (offset/partition) or
# the envelope — importing ModelEventEnvelope into a handler is a hard-fail
# (OMN-14355). idempotency does not depend on these two fields (it keys on
# terminal_event_id == event_id); only the freshness cursor's partition/
# offset segments are degraded to "0/0".
# Sourced from this node's own contract.yaml event_bus.subscribe_topics (the
# source of truth) at import time, matching node_generation_consumer's
# next((t for t in topics if "..." in t), "") idiom — never a hardcoded
# literal. Used only for freshness-cursor bookkeeping in the adapters below,
# never to construct a subscription.
_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
_contract_text = _CONTRACT_PATH.read_text()  # node-purity-ok: reads this node's own contract.yaml for topic names, same pattern as handler_savings.py's __init__
_SUBSCRIBE_TOPICS: list[str] = (
    yaml.safe_load(_contract_text).get("event_bus", {}).get("subscribe_topics", [])
)


def _topic(marker: str) -> str:
    return next((t for t in _SUBSCRIBE_TOPICS if marker in t), "")


_TOPIC_DELEGATION_EXECUTE = _topic("delegation-execute")
_TOPIC_DELEGATION_CALL_COMPLETED = _topic("delegation-call-completed")
_TOPIC_DELEGATION_ESCALATION_TRIGGERED = _topic("delegation-escalation-triggered")
_TOPIC_DELEGATION_ALL_TIERS_FAILED = _topic("delegation-all-tiers-failed")
_TOPIC_SWARM_FANOUT_COMPLETED = _topic("swarm-fanout-completed")


def _from_call_request(payload: ModelLlmDelegationCallRequest) -> ModelDelegationEvent:
    return ModelDelegationEvent(
        event_id=payload.request_id,
        event_type=EnumDelegationEventType.DELEGATION_EXECUTE,
        run_id=payload.causation_id,
        subtask_id=payload.task_id or "",
        correlation_id=payload.correlation_id,
        endpoint_id=payload.endpoint_ref,
        model_id=payload.model_id,
        source_topic=_TOPIC_DELEGATION_EXECUTE,
    )


def _from_completed(payload: ModelLlmDelegationCompletedEvent) -> ModelDelegationEvent:
    return ModelDelegationEvent(
        event_id=payload.request_id,
        event_type=EnumDelegationEventType.DELEGATION_CALL_COMPLETED,
        run_id=payload.causation_id,
        subtask_id=payload.task_id or "",
        correlation_id=payload.correlation_id,
        endpoint_id=payload.endpoint_ref,
        model_id=payload.model_id,
        latency_ms=payload.latency_ms,
        emitted_at=payload.created_at.isoformat(),
        source_topic=_TOPIC_DELEGATION_CALL_COMPLETED,
    )


def _from_escalation(
    payload: ModelLlmDelegationEscalationTriggeredEvent,
) -> ModelDelegationEvent:
    return ModelDelegationEvent(
        event_id=payload.request_id,
        event_type=EnumDelegationEventType.DELEGATION_ESCALATION_TRIGGERED,
        run_id=payload.causation_id,
        subtask_id=payload.task_id or "",
        correlation_id=payload.correlation_id,
        model_id=payload.model_id,
        failure_class=payload.failure_class.value,
        emitted_at=payload.created_at.isoformat(),
        source_topic=_TOPIC_DELEGATION_ESCALATION_TRIGGERED,
    )


def _from_all_tiers_failed(
    payload: ModelLlmDelegationAllTiersFailedEvent,
) -> ModelDelegationEvent:
    # The last attempted model's failure class is the one whose ceiling
    # failed — mirrors node_delegation_routing_feedback_reducer's
    # _resolve_model_id convention for the same "no single model_id" case.
    failure_class = payload.failure_classes[-1].value if payload.failure_classes else ""
    return ModelDelegationEvent(
        event_id=payload.request_id,
        event_type=EnumDelegationEventType.DELEGATION_ALL_TIERS_FAILED,
        run_id=payload.causation_id,
        subtask_id=payload.task_id or "",
        correlation_id=payload.correlation_id,
        failure_class=failure_class,
        emitted_at=payload.created_at.isoformat(),
        source_topic=_TOPIC_DELEGATION_ALL_TIERS_FAILED,
    )


def _from_fanout_completed(payload: ModelSwarmFanoutResult) -> ModelDelegationEvent:
    # Whole-run terminal: delta() short-circuits on this event_type before
    # touching subtask_id, so a per-subtask identity is not needed here.
    return ModelDelegationEvent(
        event_id=f"fanout-completed-{payload.run_id}",
        event_type=EnumDelegationEventType.SWARM_FANOUT_COMPLETED,
        run_id=payload.run_id,
        subtask_id="",
        correlation_id=payload.run_id,
        source_topic=_TOPIC_SWARM_FANOUT_COMPLETED,
    )


_Adapter = Callable[[Any], ModelDelegationEvent]
_ADAPTERS: tuple[tuple[type[Any], _Adapter], ...] = (
    (ModelLlmDelegationCallRequest, _from_call_request),
    (ModelLlmDelegationCompletedEvent, _from_completed),
    (ModelLlmDelegationEscalationTriggeredEvent, _from_escalation),
    (ModelLlmDelegationAllTiersFailedEvent, _from_all_tiers_failed),
    (ModelSwarmFanoutResult, _from_fanout_completed),
)


def _adapt(payload: object) -> ModelDelegationEvent:
    """Adapt one of the 5 real producer wire models into ModelDelegationEvent.

    Raises TypeError for any other input shape (fail-fast — a payload that
    doesn't match a declared event_model is a wiring defect, not a
    degradable condition).
    """
    if isinstance(payload, ModelDelegationEvent):
        return payload
    for model_cls, adapter in _ADAPTERS:
        if isinstance(payload, model_cls):
            return adapter(payload)
    raise TypeError(
        f"HandlerSwarmSubtaskState.handle() received unrecognized payload type "
        f"{type(payload).__name__!r} — expected one of "
        f"{[c.__name__ for c, _ in _ADAPTERS]!r} or ModelDelegationEvent."
    )


class HandlerSwarmSubtaskState:
    """Pure reducer for per-subtask lifecycle state.

    Idempotent: replaying a terminal event with the same terminal_event_id
    produces no state change and no duplicate projection-applied emission.
    """

    @property
    def handler_type(self) -> HandlerType:
        return "NODE_HANDLER"

    @property
    def handler_category(self) -> HandlerCategory:
        return "COMPUTE"

    def handle(self, input_data: object) -> dict[str, Any]:
        """Runtime dispatch entrypoint (def B: handle(request) -> response).

        ``input_data`` is whatever the contract's per-topic ``event_model``
        validated it into: one of the 5 real producer wire models declared in
        contract.yaml's handler_routing (see the ``_from_*`` adapters above),
        or — for direct/test callers — a pre-built ``ModelDelegationEvent`` or
        the raw dict shape a caller controls entirely (``{"event": ...,
        "current_state": ...}``). There is no runtime-supplied "current
        state": this reducer is not (yet) bound to OMN-14208 state_io, so
        every dispatch reduces against a fresh empty run state unless a dict
        caller supplies one explicitly.
        """
        if isinstance(input_data, dict):
            inp = ModelSwarmSubtaskReducerInput(**input_data)
        else:
            inp = ModelSwarmSubtaskReducerInput(event=_adapt(input_data))
        result = self.delta(inp)
        return result.model_dump(mode="json")

    def delta(
        self,
        inp: ModelSwarmSubtaskReducerInput,
    ) -> ModelSwarmSubtaskReducerOutput:
        """Compute next run state from current state + delegation event.

        Returns unchanged state (state_changed=False) when:
        - event type is swarm_fanout_completed (whole-run terminal, not per-subtask)
        - the subtask is already in a terminal state and this is not a new terminal event
        - idempotency: same terminal_event_id already processed
        """
        event = inp.event
        current = inp.current_state or _empty_run_state(event.run_id)

        # swarm-fanout-completed is whole-run terminal; no per-subtask transition
        if event.event_type == EnumDelegationEventType.SWARM_FANOUT_COMPLETED:
            return ModelSwarmSubtaskReducerOutput(
                new_state=current,
                state_changed=False,
            )

        target_state = _EVENT_TO_STATE.get(event.event_type)
        if target_state is None:
            logger.warning("Unhandled event type: %s", event.event_type)
            return ModelSwarmSubtaskReducerOutput(
                new_state=current,
                state_changed=False,
            )

        existing = current.subtasks.get(event.subtask_id)

        # Idempotency: if subtask is terminal and this event_id already recorded, no-op
        if existing is not None and existing.state in TERMINAL_STATES:
            if existing.terminal_event_id == event.event_id:
                logger.debug(
                    "Idempotent replay: subtask=%s event_id=%s already terminal",
                    event.subtask_id,
                    event.event_id,
                )
                return ModelSwarmSubtaskReducerOutput(
                    new_state=current,
                    state_changed=False,
                )
            # A new terminal event on an already-terminal subtask: log and skip
            # (BLOCKED may arrive after FAILED from a different code path)
            if target_state in TERMINAL_STATES:
                logger.warning(
                    "Subtask %s already terminal (%s); ignoring %s",
                    event.subtask_id,
                    existing.state,
                    target_state,
                )
                return ModelSwarmSubtaskReducerOutput(
                    new_state=current,
                    state_changed=False,
                )

        new_subtask = _build_subtask_state(event, existing, target_state)
        new_subtasks = dict(current.subtasks)
        new_subtasks[event.subtask_id] = new_subtask

        new_run_state = ModelSwarmRunState(
            run_id=current.run_id,
            subtasks=new_subtasks,
            total_count=max(current.total_count, len(new_subtasks)),
        )

        projection_cursor = (
            f"{event.source_topic}/{event.source_partition}/{event.source_offset}"
        )
        freshness = ModelProjectionFreshness(
            projection_cursor=projection_cursor,
            source_event_id=event.event_id,
            source_topic=event.source_topic,
            source_partition=event.source_partition,
            source_offset=event.source_offset,
            freshness_state=EnumFreshnessState.FRESH,
            reducer_version=REDUCER_VERSION,
            observed_at=event.emitted_at,
        )

        logger.info(
            "Subtask transition: run=%s subtask=%s %s -> %s",
            event.run_id,
            event.subtask_id,
            existing.state if existing else "new",
            target_state,
        )

        return ModelSwarmSubtaskReducerOutput(
            new_state=new_run_state,
            changed_subtask=new_subtask,
            state_changed=True,
            projection_freshness=freshness,
        )


def _empty_run_state(run_id: str) -> ModelSwarmRunState:
    return ModelSwarmRunState(run_id=run_id, subtasks={}, total_count=0)


def _build_subtask_state(
    event: ModelDelegationEvent,
    existing: ModelSubtaskState | None,
    target_state: EnumSubtaskState,
) -> ModelSubtaskState:
    attempt_count = (existing.attempt_count if existing else 0) + (
        1
        if event.event_type == EnumDelegationEventType.DELEGATION_ESCALATION_TRIGGERED
        else 0
    )
    if existing is None:
        attempt_count = (
            1 if event.event_type == EnumDelegationEventType.DELEGATION_EXECUTE else 0
        )

    terminal_event_id = (
        event.event_id
        if target_state in TERMINAL_STATES
        else (existing.terminal_event_id if existing else "")
    )

    return ModelSubtaskState(
        run_id=event.run_id,
        subtask_id=event.subtask_id,
        state=target_state,
        endpoint_id=event.endpoint_id or (existing.endpoint_id if existing else ""),
        model_id=event.model_id or (existing.model_id if existing else ""),
        assigned_at=(
            event.emitted_at
            if event.event_type == EnumDelegationEventType.DELEGATION_EXECUTE
            else (existing.assigned_at if existing else "")
        ),
        completed_at=(
            event.emitted_at
            if target_state in TERMINAL_STATES
            else (existing.completed_at if existing else "")
        ),
        latency_ms=event.latency_ms or (existing.latency_ms if existing else 0),
        attempt_count=attempt_count,
        failure_class=event.failure_class
        or (existing.failure_class if existing else ""),
        terminal_event_id=terminal_event_id,
    )
