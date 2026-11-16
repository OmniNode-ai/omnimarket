# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_dev_roll_window_gate_effect over its real contract on the in-memory bus.

Three dev deploy commands in, three held events, nothing toward the deploy agent; one tick after the
window, the three commands back out on the deploy-publish topic oldest first with the released flag, and
one roll_now decision; the agent's completion ends the roll. Topic bytes in, typed models out, through
the runtime's own path: discovery reads ``contract.yaml``, ``build_routing_map`` resolves each subscribed
topic to its handler and ``event_model``, ``RuntimeDispatch`` decodes the envelope and the handler's
return goes out on the topic ``published_events`` names for its class.
"""

from __future__ import annotations

import pytest

from omnimarket.events.runtime_deployment import ModelDeployPublishCommand
from omnimarket.nodes.node_dev_roll_window_gate_effect.models import (
    ModelDevRollGateDecision,
    ModelDevRollGateHeld,
    ModelDevRollGateRollRecorded,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.chain_support import (
    COMPLETED_TOPIC,
    DECISION_TOPIC,
    DEPLOY_PUBLISH_TOPIC,
    HELD_TOPIC,
    HOLD_TOPIC,
    KEY,
    RECORDED_TOPIC,
    REFUSED_TOPIC,
    TICK_TOPIC,
    chain,
    envelope,
    on,
    tick_payload,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.conftest import (
    POLICY,
    WINDOW,
    Gate,
    command,
    completed,
)

pytestmark = pytest.mark.unit


async def _send_command(transport, item: ModelDeployPublishCommand) -> None:
    await transport.send(
        HOLD_TOPIC,
        key=KEY,
        value=envelope(item.model_dump(mode="json"), "ModelDeployPublishCommand"),
        headers={},
    )


async def _send_tick(transport, gate: Gate) -> None:
    await transport.send(
        TICK_TOPIC,
        key=KEY,
        value=envelope(tick_payload(gate.clock.now), "ModelRuntimeTick"),
        headers={},
    )


def test_the_subscribed_topics_are_the_contracts_three() -> None:
    _d, _t, _b, topics = chain(Gate())
    assert topics == {HOLD_TOPIC, TICK_TOPIC, COMPLETED_TOPIC}


async def test_golden_three_commands_are_held_then_released_together_in_order() -> None:
    gate = Gate()
    dispatch, transport, broker, _ = chain(gate)
    sent = [command(number=n, age_seconds=WINDOW - 60 * n) for n in (1, 2, 3)]
    for item in sent:
        await _send_command(transport, item)
    assert await dispatch.drain() == 3
    held = [
        ModelDevRollGateHeld.model_validate(m.payload) for m in on(broker, HELD_TOPIC)
    ]
    assert [h.held_count for h in held] == [1, 2, 3]
    assert on(broker, DEPLOY_PUBLISH_TOPIC) == []

    gate.clock.advance(WINDOW)
    await _send_tick(transport, gate)
    assert await dispatch.drain() == 1
    out = [
        ModelDeployPublishCommand.model_validate(m.payload)
        for m in on(broker, DEPLOY_PUBLISH_TOPIC)
    ]
    assert [c.correlation_id for c in out] == [c.correlation_id for c in sent]
    assert all(c.roll_window_released for c in out)
    (decision,) = [
        ModelDevRollGateDecision.model_validate(m.payload)
        for m in on(broker, DECISION_TOPIC)
    ]
    assert decision.action == "roll_now"
    assert decision.released_count == 3
    assert on(broker, REFUSED_TOPIC) == []


async def test_golden_the_agents_completion_ends_the_roll_and_is_recorded() -> None:
    gate = Gate()
    dispatch, transport, broker, _ = chain(gate)
    item = command(number=1, age_seconds=WINDOW)
    await _send_command(transport, item)
    await _send_tick(transport, gate)
    while await dispatch.drain():
        pass
    gate.clock.advance(300)
    done = completed(item.correlation_id, at=gate.clock.now)
    await transport.send(
        COMPLETED_TOPIC,
        key=KEY,
        value=envelope(done.model_dump(mode="json"), "ModelDeployRebuildCompleted"),
        headers={},
    )
    assert await dispatch.drain() == 1
    (recorded,) = [
        ModelDevRollGateRollRecorded.model_validate(m.payload)
        for m in on(broker, RECORDED_TOPIC)
    ]
    assert recorded.outcome == "finished_healthy"
    assert gate.store.load().last_roll is not None
    assert gate.store.load().last_roll.finished_at == done.completed_at


async def test_golden_a_tick_with_nothing_held_publishes_nothing() -> None:
    gate = Gate()
    dispatch, transport, broker, _ = chain(gate)
    await _send_tick(transport, gate)
    assert await dispatch.drain() == 1
    assert on(broker, DECISION_TOPIC) == []
    assert on(broker, DEPLOY_PUBLISH_TOPIC) == []


async def test_failing_control_a_gate_that_ignores_the_window_releases_early_and_the_golden_assertion_fails() -> (
    None
):
    """The window assertion is real: with a zero-length window the same early tick releases, so asserting 'nothing released' fails."""
    gate = Gate(POLICY.model_copy(update={"batch_window_seconds": 1}))
    dispatch, transport, broker, _ = chain(gate)
    await _send_command(transport, command(number=1, age_seconds=10))
    await _send_tick(transport, gate)
    while await dispatch.drain():
        pass
    with pytest.raises(AssertionError):
        assert on(broker, DEPLOY_PUBLISH_TOPIC) == []
