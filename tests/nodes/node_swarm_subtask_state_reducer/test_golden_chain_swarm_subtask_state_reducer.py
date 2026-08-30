# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_swarm_subtask_state_reducer (OMN-16939).

The chain this proves is the one that was broken in production: a REAL
``onex.evt.omnimarket.delegation-escalation-triggered.v1`` producer wire message
(``ModelLlmDelegationEscalationTriggeredEvent``, exactly the model the contract declares
as that topic's ``event_model``) enters ``handle()`` and drives the FSM
``assigned -> escalating``.

Why this file exists rather than another FSM unit test: on the .201 dev lane this leg was
consuming, DLQ'ing and COMMITTING 128 messages per 40 minutes at LAG 0. The contract had
the full per-topic ``topic_match`` split AND per-topic ``event_model``s — everything the
OMN-14605 fix pattern prescribes — and every one of its four ``.evt.`` topics was still
permanently NO_DISPATCHER, because ``_prepare_handler_wiring`` derives an entry's message
category ONCE (explicit ``message_category``, else ``subscribe_topics[0]``, which here is
a ``.cmd.`` topic) and stamps it on every route. So the two halves are asserted together:

1. the contract-level routing invariant — every declared ``subscribe_topic`` has an entry
   that names it AND declares the category matching the topic's own ``.cmd.``/``.evt.``
   prefix (the assertion that goes red if the fix is reverted); and
2. the behavioural chain — the real wire model actually reduces through ``handle()``.

Part 1 is deliberately expressed against the contract YAML with no cross-repo import, so
this test stays runnable in omnimarket CI on its own. The authoritative, drift-proof
version of the same invariant is the omnibase_infra ratchet
(``omnibase_infra.validators.subscriber_dispatcher_resolution``), which resolves it
through the real wiring helpers; this is its node-local companion, not a substitute.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_swarm_subtask_state_reducer.handlers.handler_swarm_subtask_state import (
    HandlerSwarmSubtaskState,
)
from omnimarket.nodes.node_swarm_subtask_state_reducer.models.model_subtask_state import (
    EnumSubtaskState,
)
from omnimarket.nodes.node_swarm_subtask_state_reducer.models.model_swarm_subtask_input import (
    EnumDelegationEventType,
    ModelDelegationEvent,
    ModelSwarmSubtaskReducerInput,
)

_CONTRACT = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_swarm_subtask_state_reducer"
    / "contract.yaml"
)

_ESCALATION_TOPIC = "onex.evt.omnimarket.delegation-escalation-triggered.v1"


def _category_for(topic: str) -> str:
    """The category a message on ``topic`` actually arrives under."""
    kind = topic.split(".")[1]
    return {"evt": "event", "cmd": "command", "intent": "intent"}[kind]


@pytest.mark.unit
def test_every_subscribe_topic_declares_its_own_matching_category() -> None:
    """RED if the OMN-16939 fix is reverted: each topic must own an entry in its category.

    An entry without an explicit ``message_category`` inherits the category derived from
    ``subscribe_topics[0]`` — a ``.cmd.`` topic here — so all four event topics would
    register as ``command`` and never match a real event.
    """
    contract = yaml.safe_load(_CONTRACT.read_text())
    subscribe_topics = contract["event_bus"]["subscribe_topics"]
    entries = contract["handler_routing"]["handlers"]

    assert subscribe_topics, "contract declares no subscribe topics"
    # The trap this guards: subscribe_topics[0] is a .cmd. topic, so the fallback category
    # for any entry that omits message_category is 'command'.
    assert _category_for(subscribe_topics[0]) == "command"

    by_topic = {e["topic"]: e for e in entries if e.get("topic")}
    for topic in subscribe_topics:
        entry = by_topic.get(topic)
        assert entry is not None, f"{topic} has no handler_routing entry naming it"
        assert "message_category" in entry, (
            f"{topic} declares no explicit message_category; it would inherit "
            f"{_category_for(subscribe_topics[0])!r} from subscribe_topics[0] and be "
            f"permanently NO_DISPATCHER (OMN-16939)"
        )
        assert entry["message_category"] == _category_for(topic), (
            f"{topic} registers under {entry['message_category']!r} but messages arrive "
            f"as {_category_for(topic)!r}"
        )


@pytest.mark.unit
def test_escalation_topic_declares_the_real_producer_wire_model() -> None:
    """The escalation entry is type-scoped on the model its producer actually emits."""
    contract = yaml.safe_load(_CONTRACT.read_text())
    entry = next(
        e
        for e in contract["handler_routing"]["handlers"]
        if e.get("topic") == _ESCALATION_TOPIC
    )
    assert entry["event_model"]["name"] == "ModelLlmDelegationEscalationTriggeredEvent"


@pytest.mark.unit
def test_escalation_event_reduces_assigned_to_escalating() -> None:
    """The behavioural chain: the escalation event drives assigned -> escalating.

    This is the transition that could never fire on the dev lane, because the message was
    DLQ'd before ``handle()`` was ever called.
    """
    handler = HandlerSwarmSubtaskState()

    assigned = handler.delta(
        ModelSwarmSubtaskReducerInput(
            event=ModelDelegationEvent(
                event_id="gc-omn16939-1",
                event_type=EnumDelegationEventType.DELEGATION_EXECUTE,
                run_id="run-gc-omn16939",
                subtask_id="sub-gc-omn16939",
                correlation_id="run-gc-omn16939-sub-gc-omn16939",
            )
        )
    )
    assert assigned.changed_subtask is not None
    assert assigned.changed_subtask.state == EnumSubtaskState.ASSIGNED

    escalated = handler.delta(
        ModelSwarmSubtaskReducerInput(
            event=ModelDelegationEvent(
                event_id="gc-omn16939-2",
                event_type=EnumDelegationEventType.DELEGATION_ESCALATION_TRIGGERED,
                run_id="run-gc-omn16939",
                subtask_id="sub-gc-omn16939",
                correlation_id="run-gc-omn16939-sub-gc-omn16939",
            ),
            current_state=assigned.new_state,
        )
    )
    assert escalated.state_changed is True
    assert escalated.changed_subtask is not None
    assert escalated.changed_subtask.state == EnumSubtaskState.ESCALATING


@pytest.mark.unit
def test_handle_accepts_the_declared_escalation_wire_model() -> None:
    """``handle()`` adapts the contract-declared producer model, not an invented shape.

    The dispatcher validates the incoming payload into the entry's ``event_model`` before
    calling ``handle()``, so the chain is only real if the handler accepts that exact type.
    """
    from datetime import UTC, datetime

    from omnimarket.models.delegation.llm_cost_routing.model_llm_delegation_escalation_triggered_event import (
        ModelLlmDelegationEscalationTriggeredEvent,
    )

    wire = ModelLlmDelegationEscalationTriggeredEvent(
        correlation_id="run-gc-omn16939-sub-gc-omn16939",
        causation_id="gc-omn16939-cause",
        request_id="sub-gc-omn16939",
        task_type="codegen",
        task_id="sub-gc-omn16939",
        model_id="tier-1-model",
        attempt_number=1,
        failure_class="rate_limited",
        escalation_reason="tier 1 rate limited; escalating",
        next_model_id="tier-2-model",
        created_at=datetime.now(tz=UTC),
    )

    result = HandlerSwarmSubtaskState().handle(wire)

    assert isinstance(result, dict)
    assert "new_state" in result, result
    # The adapter mapped the wire model onto the escalation transition, not onto the
    # unhandled-event no-op path.
    assert result["state_changed"] is True, result
