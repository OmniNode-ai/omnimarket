# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain for node_dev_roll_window_gate_effect over its real contract on the in-memory bus.

Every command the gate cannot hold yields one typed refusal event and is passed straight on to the
deploy-publish topic marked released (never dropped); a command already released is refused alone; a
corrupt store releases nothing and raises; a tick that the batching decision holds publishes no command;
the state is saved before the released commands are returned.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.events.runtime_deployment import (
    EnumRuntimeLane,
    ModelDeployPublishCommand,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.handlers import (
    FileDevRollGateStore,
    HandlerDevRollWindowHold,
    HandlerDevRollWindowTick,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.models import (
    EnumDevRollGateRefusal,
    ModelDevRollGateDecision,
    ModelDevRollGateRefused,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.chain_support import (
    DECISION_TOPIC,
    DEPLOY_PUBLISH_TOPIC,
    HELD_TOPIC,
    HOLD_TOPIC,
    KEY,
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
    Clock,
    Gate,
    command,
)

pytestmark = pytest.mark.unit


async def _send(transport, item: ModelDeployPublishCommand) -> None:
    await transport.send(
        HOLD_TOPIC,
        key=KEY,
        value=envelope(item.model_dump(mode="json"), "ModelDeployPublishCommand"),
        headers={},
    )


def _refusals(broker) -> list[ModelDevRollGateRefused]:
    return [
        ModelDevRollGateRefused.model_validate(m.payload)
        for m in on(broker, REFUSED_TOPIC)
    ]


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (
            lambda: command(lane=EnumRuntimeLane.STABILITY_TEST),
            EnumDevRollGateRefusal.NOT_A_DEV_COMMAND,
        ),
        (
            lambda: command(requested_by="node_redeploy_orchestrator"),
            EnumDevRollGateRefusal.REQUESTER_NOT_A_CI_ISSUER,
        ),
        (
            lambda: command(requested_by="gha/omniclaude/pr-3"),
            EnumDevRollGateRefusal.REPOSITORY_UNKNOWN,
        ),
        (
            lambda: command(git_ref="origin/dev"),
            EnumDevRollGateRefusal.GIT_REF_NOT_A_SHA,
        ),
        (
            lambda: command(image_digest="sha256:" + "a" * 64),
            EnumDevRollGateRefusal.PINNED_IMAGE,
        ),
    ],
)
async def test_error_a_command_the_gate_cannot_hold_is_refused_then_passed_on(
    make, reason: EnumDevRollGateRefusal
) -> None:
    gate = Gate()
    dispatch, transport, broker, _ = chain(gate)
    sent = make()
    await _send(transport, sent)
    assert await dispatch.drain() == 1
    (refusal,) = _refusals(broker)
    assert refusal.reason is reason
    assert refusal.command_correlation_id == sent.correlation_id
    (passed,) = [
        ModelDeployPublishCommand.model_validate(m.payload)
        for m in on(broker, DEPLOY_PUBLISH_TOPIC)
    ]
    assert passed.correlation_id == sent.correlation_id
    assert passed.roll_window_released is True
    assert on(broker, HELD_TOPIC) == []
    assert gate.store.load().held == ()


async def test_error_the_gate_off_passes_a_dev_command_on_with_a_gate_off_refusal() -> (
    None
):
    gate = Gate(POLICY.model_copy(update={"mode": "off"}))
    dispatch, transport, broker, _ = chain(gate)
    await _send(transport, command(number=1))
    assert await dispatch.drain() == 1
    (refusal,) = _refusals(broker)
    assert refusal.reason is EnumDevRollGateRefusal.GATE_OFF
    assert len(on(broker, DEPLOY_PUBLISH_TOPIC)) == 1


async def test_error_a_full_store_passes_the_next_command_on() -> None:
    gate = Gate(POLICY.model_copy(update={"held_kept": 2}))
    dispatch, transport, broker, _ = chain(gate)
    for n in (1, 2, 3):
        await _send(transport, command(number=n))
    while await dispatch.drain():
        pass
    (refusal,) = _refusals(broker)
    assert refusal.reason is EnumDevRollGateRefusal.HELD_FULL
    assert len(gate.store.load().held) == 2
    assert len(on(broker, DEPLOY_PUBLISH_TOPIC)) == 1


async def test_error_a_command_already_released_is_refused_alone_and_never_published_twice() -> (
    None
):
    gate = Gate()
    dispatch, transport, broker, _ = chain(gate)
    item = command(number=1, age_seconds=WINDOW)
    await _send(transport, item)
    await transport.send(
        TICK_TOPIC,
        key=KEY,
        value=envelope(tick_payload(gate.clock.now), "ModelRuntimeTick"),
        headers={},
    )
    while await dispatch.drain():
        pass
    assert len(on(broker, DEPLOY_PUBLISH_TOPIC)) == 1
    await _send(transport, item)  # the same command redelivered to the hold topic
    while await dispatch.drain():
        pass
    (refusal,) = _refusals(broker)
    assert refusal.reason is EnumDevRollGateRefusal.ALREADY_RELEASED
    assert len(on(broker, DEPLOY_PUBLISH_TOPIC)) == 1
    assert gate.store.load().held == ()


async def test_error_a_tick_the_decision_holds_publishes_no_command() -> None:
    gate = Gate()
    dispatch, transport, broker, _ = chain(gate)
    await _send(transport, command(number=1, age_seconds=10))
    await transport.send(
        TICK_TOPIC,
        key=KEY,
        value=envelope(tick_payload(gate.clock.now), "ModelRuntimeTick"),
        headers={},
    )
    while await dispatch.drain():
        pass
    (decision,) = [
        ModelDevRollGateDecision.model_validate(m.payload)
        for m in on(broker, DECISION_TOPIC)
    ]
    assert (decision.action, decision.hold_reason) == ("hold", "window_open")
    assert on(broker, DEPLOY_PUBLISH_TOPIC) == []
    assert len(gate.store.load().held) == 1


def test_error_a_corrupt_file_store_raises_and_releases_nothing(tmp_path: Path) -> None:
    store = FileDevRollGateStore(tmp_path)
    (tmp_path / "gate-state.json").write_text("not json")
    clock = Clock()
    tick = HandlerDevRollWindowTick(store, policy=POLICY, clock=clock)
    hold = HandlerDevRollWindowHold(store, policy=POLICY, clock=clock)
    with pytest.raises(ValueError, match="unreadable"):
        tick.handle(_a_tick(clock))
    with pytest.raises(ValueError, match="unreadable"):
        hold.handle(command(number=1))


def _a_tick(clock: Clock) -> ModelRuntimeTick:
    return ModelRuntimeTick.model_validate(tick_payload(clock.now))


def test_error_the_state_is_saved_before_the_released_commands_are_returned() -> None:
    gate = Gate()
    gate.hold.handle(command(number=1, age_seconds=WINDOW))
    seen: list[int] = []
    real_save = gate.store.save

    def spy(state) -> None:
        real_save(state)
        seen.append(len(state.held))

    gate.store.save = spy  # a spy on the injected store, no monkeypatch of the node
    out = gate.tick.handle(_a_tick(gate.clock))
    assert seen == [0]  # saved with the command already removed from held
    assert out is not None
    assert len(out) == 2


def test_error_when_the_save_fails_nothing_is_released() -> None:
    gate = Gate()
    gate.hold.handle(command(number=1, age_seconds=WINDOW))

    def refuse(state) -> None:
        raise OSError("disk full")

    gate.store.save = refuse  # the injected store fails; the node is not patched
    with pytest.raises(OSError, match="disk full"):
        gate.tick.handle(_a_tick(gate.clock))
    assert len(gate.store.load().held) == 1


def test_failing_control_with_a_working_store_the_same_tick_does_release() -> None:
    """The refusal above is real: the identical call on a working store returns the command and the decision."""
    gate = Gate()
    gate.hold.handle(command(number=1, age_seconds=WINDOW))
    out = gate.tick.handle(_a_tick(gate.clock))
    assert out is not None
    assert len(out) == 2
