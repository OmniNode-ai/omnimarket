# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The deploy effect and the roll gate (OMN-24431 AC3).

A dev command is forwarded to the gate only when the gate overlay says live; with the
overlay off or absent it is published to the deploy agent exactly as before; and the
gate's release is published without being held again.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory

from omnimarket.events.runtime_deployment import (
    EnumRuntimeLane,
    ModelDeployPublishCommand,
)
from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.models.dev_roll import ModelDevRollReleased
from omnimarket.nodes.node_redeploy_deploy_effect.handlers import (
    handler_deploy_publish_monitor as effect,
)
from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
    TOPIC_DEV_ROLL_HOLD_REQUESTED,
    TOPIC_REBUILD_REQUESTED,
    HandlerDeployPublishMonitor,
)
from omnimarket.nodes.node_redeploy_orchestrator.handlers.handler_redeploy_orchestrator import (
    TOPIC_DEPLOY_PUBLISH,
)
from omnimarket.testing.publisher_contract_fixture import publisher_event_type
from tests.unit.nodes.node_dev_roll_gate_effect.chain_support import (
    RELEASED_TOPIC as GATE_RELEASED_TOPIC,
)
from tests.unit.nodes.node_dev_roll_gate_effect.chain_support import (
    join_chain,
    on,
    pump,
    send_hold,
    send_tick,
)
from tests.unit.nodes.node_dev_roll_gate_effect.conftest import (
    GATE_DOC,
    NOW,
    WINDOW_SECONDS,
    command,
)

pytestmark = pytest.mark.unit

RELEASED_TOPIC = "onex.evt.omnimarket.dev-roll-released.v1"


class _RecordingBus(EventBusInmemory):
    """The in-memory bus, keeping every publish by topic."""

    def __init__(self) -> None:
        super().__init__(environment="test", group="omn24431")
        self.sent: dict[str, list[Any]] = {}

    async def publish(self, topic: str, key: Any, value: Any, **kwargs: Any) -> Any:
        self.sent.setdefault(topic, []).append(json.loads(value))
        return await super().publish(topic, key, value, **kwargs)


def _overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, doc: object | None
) -> None:
    node_dir = tmp_path / "node_dev_roll_gate_effect"
    node_dir.mkdir(parents=True)
    if doc is not None:
        (node_dir / "overlay.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))


def _command_envelope(held: ModelDeployPublishCommand) -> ModelEventEnvelope[Any]:
    return ModelEventEnvelope(
        payload=held,
        correlation_id=held.correlation_id,
        event_type=publisher_event_type(TOPIC_DEPLOY_PUBLISH),
    )


def _release_envelope(
    commands: tuple[ModelDeployPublishCommand, ...],
) -> ModelEventEnvelope[Any]:
    released = ModelDevRollReleased(
        release_id=uuid4(),
        decision_correlation_id=uuid4(),
        released_at=NOW,
        commands=commands,
    )
    return ModelEventEnvelope(
        payload=released.model_dump(mode="json"),
        correlation_id=released.release_id,
        event_type=publisher_event_type(RELEASED_TOPIC),
    )


async def _handle(envelope: ModelEventEnvelope[Any]) -> _RecordingBus:
    bus = _RecordingBus()
    await bus.start()
    try:
        await HandlerDeployPublishMonitor(event_bus=bus).handle(envelope)
    finally:
        await bus.close()
    return bus


async def test_ac3_live_forwards_a_dev_command_to_the_gate_and_publishes_nothing_to_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _overlay(tmp_path, monkeypatch, GATE_DOC)
    held = command()
    bus = await _handle(_command_envelope(held))
    assert TOPIC_REBUILD_REQUESTED not in bus.sent
    (forward,) = bus.sent[TOPIC_DEV_ROLL_HOLD_REQUESTED]
    assert forward["payload"]["command"]["correlation_id"] == str(held.correlation_id)


@pytest.mark.parametrize("mode", ["off"])
async def test_ac3_off_publishes_exactly_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    _overlay(tmp_path, monkeypatch, {**GATE_DOC, "mode": mode})
    held = command()
    bus = await _handle(_command_envelope(held))
    assert TOPIC_DEV_ROLL_HOLD_REQUESTED not in bus.sent
    (published,) = bus.sent[TOPIC_REBUILD_REQUESTED]
    assert published["correlation_id"] == str(held.correlation_id)


async def test_ac3_an_absent_overlay_publishes_exactly_as_before(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _overlay(tmp_path, monkeypatch, None)
    held = command()
    bus = await _handle(_command_envelope(held))
    assert TOPIC_DEV_ROLL_HOLD_REQUESTED not in bus.sent
    assert len(bus.sent[TOPIC_REBUILD_REQUESTED]) == 1


async def test_ac3_a_broken_overlay_is_refused_not_read_as_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _overlay(tmp_path, monkeypatch, {"mode": "sideways"})
    with pytest.raises(NodeOverlayError):
        await _handle(_command_envelope(command()))


async def test_ac3_a_command_for_another_lane_is_never_forwarded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _overlay(tmp_path, monkeypatch, GATE_DOC)
    other = command().model_copy(
        update={"runtime_lane": EnumRuntimeLane.STABILITY_TEST}
    )
    bus = await _handle(_command_envelope(other))
    assert TOPIC_DEV_ROLL_HOLD_REQUESTED not in bus.sent
    assert len(bus.sent[TOPIC_REBUILD_REQUESTED]) == 1


async def test_ac3_a_release_is_published_in_order_and_not_held_again_while_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _overlay(tmp_path, monkeypatch, GATE_DOC)
    first, second, third = command(), command(), command()
    bus = await _handle(_release_envelope((first, second, third)))
    assert TOPIC_DEV_ROLL_HOLD_REQUESTED not in bus.sent
    assert [p["correlation_id"] for p in bus.sent[TOPIC_REBUILD_REQUESTED]] == [
        str(first.correlation_id),
        str(second.correlation_id),
        str(third.correlation_id),
    ]


async def test_ac3_a_redelivered_release_does_not_publish_a_command_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _overlay(tmp_path, monkeypatch, GATE_DOC)
    only = command()
    bus = _RecordingBus()
    await bus.start()
    try:
        handler = HandlerDeployPublishMonitor(event_bus=bus)
        await handler.handle(_release_envelope((only,)))
        await handler.handle(_release_envelope((only,)))
    finally:
        await bus.close()
    assert len(bus.sent[TOPIC_REBUILD_REQUESTED]) == 1


async def test_ac3_a_release_naming_a_non_dev_lane_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _overlay(tmp_path, monkeypatch, GATE_DOC)
    other = command().model_copy(
        update={"runtime_lane": EnumRuntimeLane.STABILITY_TEST}
    )
    with pytest.raises(ValueError, match="dev lane"):
        await _handle(_release_envelope((other,)))


async def test_failing_control_a_handler_that_ignores_the_overlay_publishes_straight_to_the_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live-mode expectation (nothing to the agent) is red when the overlay is not consulted."""
    _overlay(tmp_path, monkeypatch, GATE_DOC)
    monkeypatch.setattr(effect, "dev_roll_gate_is_live", lambda: False)
    bus = await _handle(_command_envelope(command()))
    assert TOPIC_REBUILD_REQUESTED in bus.sent
    assert TOPIC_DEV_ROLL_HOLD_REQUESTED not in bus.sent


async def test_join_the_release_the_gate_publishes_is_the_release_the_deploy_effect_publishes_from(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store, clock, compute
) -> None:
    """The gate's own outbound envelope (runtime-stamped event_type) read by the deploy effect, in order."""
    assert GATE_RELEASED_TOPIC == RELEASED_TOPIC
    _overlay(tmp_path, monkeypatch, GATE_DOC)
    dispatch, transport, broker, _ = join_chain(store, clock, compute)
    first, second = command(), command()
    for held in (first, second):
        await send_hold(transport, held)
        await pump(dispatch)
        clock.advance(10)
    clock.advance(WINDOW_SECONDS)
    await send_tick(transport)
    await pump(dispatch)
    (released,) = on(broker, GATE_RELEASED_TOPIC)
    bus = await _handle(released)
    assert TOPIC_DEV_ROLL_HOLD_REQUESTED not in bus.sent
    assert [p["correlation_id"] for p in bus.sent[TOPIC_REBUILD_REQUESTED]] == [
        str(first.correlation_id),
        str(second.correlation_id),
    ]
