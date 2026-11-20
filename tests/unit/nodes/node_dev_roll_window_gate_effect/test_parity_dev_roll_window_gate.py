# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity of node_dev_roll_window_gate_effect on the in-memory bus and on a real broker (OMN-24431, AC4).

The golden chain, the error chain, the join of the deploy effect and the gate, and the seam controls run over
the gate's real contract and the runtime's own dispatch twice: on the in-memory transport always, and on a
Redpanda broker through ``KafkaTransport`` when ``DEV_ROLL_GATE_PARITY_BOOTSTRAP`` names a plaintext lab
broker the caller started for the proof (never the dev lane's):

    DEV_ROLL_GATE_PARITY_BOOTSTRAP=<host:port> uv run pytest \
        tests/unit/nodes/node_dev_roll_window_gate_effect/test_parity_dev_roll_window_gate.py -m dev_roll_gate_parity -q

The seam controls each rename one field the gate reads (the deploy command's ``git_ref``, the completion event's
``completed_at``) or drop the envelope, and the golden expectation goes red on both buses.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_core.runtime.runtime_dispatch import RuntimeDispatch
from omnibase_infra.runtime.core_runtime.dlq_resolver import derive_canonical_dlq_topic

from omnimarket.events.runtime_deployment import (
    EnumRuntimeLane,
    ModelDeployPublishCommand,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.models import (
    EnumDevRollGateRefusal,
    ModelDevRollGateDecision,
    ModelDevRollGateHeld,
    ModelDevRollGateRefused,
    ModelDevRollGateRollRecorded,
)
from omnimarket.nodes.node_redeploy_deploy_effect.handlers.handler_deploy_publish_monitor import (
    TOPIC_DEV_ROLL_HOLD,
    TOPIC_REBUILD_REQUESTED,
    HandlerDeployPublishMonitor,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.chain_support import (
    COMPLETED_TOPIC,
    CONTRACT,
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
    routing_for,
    tick_payload,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.conftest import (
    WINDOW,
    Gate,
    command,
    completed,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.kafka_transport import (
    KafkaTransport,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.test_join_over_the_wire_deploy_effect_and_gate import (
    WireBus,
    install_overlay,
)

BOOTSTRAP_ENV = "DEV_ROLL_GATE_PARITY_BOOTSTRAP"
OBSERVED = (
    HELD_TOPIC,
    REFUSED_TOPIC,
    DECISION_TOPIC,
    RECORDED_TOPIC,
    DEPLOY_PUBLISH_TOPIC,
)

pytestmark = [pytest.mark.unit, pytest.mark.dev_roll_gate_parity]


class Rig:
    """The gate's contract, dispatch and bus, behind one surface for both transports."""

    def __init__(
        self,
        gate: Gate,
        dispatch: RuntimeDispatch,
        transport: Any,
        read: Callable[[str], Awaitable[list[ModelEventEnvelope[object]]]],
    ) -> None:
        self.gate = gate
        self.dispatch = dispatch
        self.transport = transport
        self.on = read

    async def settle(self) -> None:
        while await self.dispatch.drain():
            pass

    async def send(
        self, topic: str, payload: dict[str, object], payload_type: str
    ) -> None:
        await self.transport.send(
            topic, key=KEY, value=envelope(payload, payload_type), headers={}
        )

    async def tick(self) -> None:
        await self.send(
            TICK_TOPIC, tick_payload(self.gate.clock.now), "ModelRuntimeTick"
        )
        await self.settle()


async def memory_rig() -> AsyncIterator[Rig]:
    gate = Gate()
    dispatch, transport, broker, _ = chain(gate)

    async def read(topic: str) -> list[ModelEventEnvelope[object]]:
        return on(broker, topic)

    yield Rig(gate, dispatch, transport, read)


async def redpanda_rig() -> AsyncIterator[Rig]:
    bootstrap = os.environ.get(BOOTSTRAP_ENV)
    if not bootstrap:
        pytest.skip(f"Kafka not reachable: {BOOTSTRAP_ENV} names no lab broker")
    gate = Gate()
    routing_map, topics = routing_for(
        [CONTRACT],
        {
            "HandlerDevRollWindowHold": gate.hold,
            "HandlerDevRollWindowTick": gate.tick,
            "HandlerDevRollWindowCompleted": gate.completed,
        },
    )
    transport = KafkaTransport(bootstrap, consume=sorted(topics), observe=OBSERVED)
    dispatch = RuntimeDispatch(
        consumer=transport,
        producer=transport,
        routing_map=routing_map,
        dlq_topic_resolver=derive_canonical_dlq_topic,
        clock=lambda: gate.clock.now,
    )
    await transport.start()
    try:
        yield Rig(gate, dispatch, transport, transport.on)
    finally:
        await transport.close()


@pytest.fixture(
    params=[
        pytest.param("memory"),
        pytest.param(
            "redpanda",
            marks=[pytest.mark.kafka, pytest.mark.integration],
        ),
    ]
)
async def rig(request: pytest.FixtureRequest) -> AsyncIterator[Rig]:
    make = memory_rig if request.param == "memory" else redpanda_rig
    async for made in make():
        yield made


def held_of(items: list[ModelEventEnvelope[object]]) -> list[ModelDevRollGateHeld]:
    return [ModelDevRollGateHeld.model_validate(m.payload) for m in items]


def commands_of(
    items: list[ModelEventEnvelope[object]],
) -> list[ModelDeployPublishCommand]:
    return [ModelDeployPublishCommand.model_validate(m.payload) for m in items]


async def golden_hold_and_release(
    rig: Rig, rename: tuple[str, str] | None = None
) -> list[ModelDeployPublishCommand]:
    """Three dev commands held, then released together oldest first by one tick after the window."""
    sent = [command(number=n, age_seconds=WINDOW - 60 * n) for n in (1, 2, 3)]
    for item in sent:
        payload = item.model_dump(mode="json")
        if rename is not None:
            payload[rename[1]] = payload.pop(rename[0])
        await rig.send(HOLD_TOPIC, payload, "ModelDeployPublishCommand")
    await rig.settle()
    assert [h.held_count for h in held_of(await rig.on(HELD_TOPIC))] == [1, 2, 3]
    assert await rig.on(DEPLOY_PUBLISH_TOPIC) == []

    rig.gate.clock.advance(WINDOW)
    await rig.tick()
    released = commands_of(await rig.on(DEPLOY_PUBLISH_TOPIC))
    assert [c.correlation_id for c in released] == [c.correlation_id for c in sent]
    assert all(c.roll_window_released for c in released)
    (decision,) = [
        ModelDevRollGateDecision.model_validate(m.payload)
        for m in await rig.on(DECISION_TOPIC)
    ]
    assert (decision.action, decision.released_count) == ("roll_now", 3)
    return released


async def golden_completion(
    rig: Rig, rename: tuple[str, str] | None = None
) -> ModelDevRollGateRollRecorded:
    item = command(number=1, age_seconds=WINDOW)
    await rig.send(
        HOLD_TOPIC, item.model_dump(mode="json"), "ModelDeployPublishCommand"
    )
    await rig.tick()
    rig.gate.clock.advance(300)
    finished_at = rig.gate.clock.now - timedelta(seconds=60)
    payload = completed(item.correlation_id, at=finished_at).model_dump(mode="json")
    if rename is not None:
        payload[rename[1]] = payload.pop(rename[0])
    await rig.send(COMPLETED_TOPIC, payload, "ModelDeployRebuildCompleted")
    await rig.settle()
    events = await rig.on(RECORDED_TOPIC)
    assert len(events) == 1
    recorded = ModelDevRollGateRollRecorded.model_validate(events[0].payload)
    assert recorded.outcome == "finished_healthy"
    last = rig.gate.store.load().last_roll
    assert last is not None
    assert (
        last.finished_at == finished_at
    )  # the agent's own completion time, not the gate's clock
    return recorded


# ---- golden chain ----


async def test_golden_three_commands_are_held_then_released_together_in_order(
    rig: Rig,
) -> None:
    await golden_hold_and_release(rig)
    assert await rig.on(REFUSED_TOPIC) == []


async def test_golden_the_agents_completion_ends_the_roll_and_is_recorded(
    rig: Rig,
) -> None:
    await golden_completion(rig)


# ---- error chain ----


@pytest.mark.parametrize(
    ("make", "reason"),
    [
        (
            lambda: command(requested_by="gha/omniclaude/pr-3"),
            EnumDevRollGateRefusal.REPOSITORY_UNKNOWN,
        ),
        (
            lambda: command(image_digest="sha256:" + "a" * 64),
            EnumDevRollGateRefusal.PINNED_IMAGE,
        ),
        (
            lambda: command(lane=EnumRuntimeLane.STABILITY_TEST),
            EnumDevRollGateRefusal.NOT_A_DEV_COMMAND,
        ),
    ],
)
async def test_error_a_command_the_gate_cannot_hold_is_refused_then_passed_on(
    rig: Rig,
    make: Callable[[], ModelDeployPublishCommand],
    reason: EnumDevRollGateRefusal,
) -> None:
    sent = make()
    await rig.send(
        HOLD_TOPIC, sent.model_dump(mode="json"), "ModelDeployPublishCommand"
    )
    await rig.settle()
    (refusal,) = [
        ModelDevRollGateRefused.model_validate(m.payload)
        for m in await rig.on(REFUSED_TOPIC)
    ]
    assert refusal.reason is reason
    (passed,) = commands_of(await rig.on(DEPLOY_PUBLISH_TOPIC))
    assert passed.correlation_id == sent.correlation_id
    assert passed.roll_window_released is True
    assert await rig.on(HELD_TOPIC) == []


async def test_error_no_second_release_while_the_previous_roll_has_no_completion(
    rig: Rig,
) -> None:
    first = command(number=1, age_seconds=WINDOW)
    await rig.send(
        HOLD_TOPIC, first.model_dump(mode="json"), "ModelDeployPublishCommand"
    )
    await rig.tick()
    assert len(await rig.on(DEPLOY_PUBLISH_TOPIC)) == 1

    rig.gate.clock.advance(WINDOW)
    second = command(number=2, age_seconds=WINDOW)
    await rig.send(
        HOLD_TOPIC, second.model_dump(mode="json"), "ModelDeployPublishCommand"
    )
    await rig.tick()
    assert [
        c.correlation_id for c in commands_of(await rig.on(DEPLOY_PUBLISH_TOPIC))
    ] == [first.correlation_id]
    decisions = [
        ModelDevRollGateDecision.model_validate(m.payload)
        for m in await rig.on(DECISION_TOPIC)
    ]
    assert [d.action for d in decisions] == ["roll_now", "hold"]
    assert len(rig.gate.store.load().held) == 1


async def test_error_a_command_already_released_is_refused_alone_and_never_published_twice(
    rig: Rig,
) -> None:
    item = command(number=1, age_seconds=WINDOW)
    await rig.send(
        HOLD_TOPIC, item.model_dump(mode="json"), "ModelDeployPublishCommand"
    )
    await rig.tick()
    await rig.send(
        HOLD_TOPIC, item.model_dump(mode="json"), "ModelDeployPublishCommand"
    )
    await rig.settle()
    (refusal,) = [
        ModelDevRollGateRefused.model_validate(m.payload)
        for m in await rig.on(REFUSED_TOPIC)
    ]
    assert refusal.reason is EnumDevRollGateRefusal.ALREADY_RELEASED
    assert len(await rig.on(DEPLOY_PUBLISH_TOPIC)) == 1


# ---- join: the deploy effect's own bytes through the gate and back ----


async def test_join_the_effects_hold_bytes_reach_the_gate_and_the_release_reaches_the_agent(
    rig: Rig, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_overlay(tmp_path, monkeypatch)
    wire = WireBus()
    effect = HandlerDeployPublishMonitor(wire)
    sent = [command(number=n, age_seconds=WINDOW - 60 * n) for n in (1, 2)]
    for item in sent:
        await effect.handle(
            ModelEventEnvelope(
                payload=item,
                correlation_id=item.correlation_id,
                event_type=DEPLOY_PUBLISH_TOPIC,
            )
        )
    held_bytes = wire.on(TOPIC_DEV_ROLL_HOLD)
    assert len(held_bytes) == 2
    assert wire.on(TOPIC_REBUILD_REQUESTED) == []
    for value in held_bytes:
        await rig.transport.send(HOLD_TOPIC, key=KEY, value=value, headers={})
    await rig.settle()
    assert len(await rig.on(HELD_TOPIC)) == 2
    assert await rig.on(REFUSED_TOPIC) == []

    rig.gate.clock.advance(WINDOW)
    await rig.tick()
    released = await rig.on(DEPLOY_PUBLISH_TOPIC)
    assert len(released) == 2
    for message in released:
        await effect.handle(message)
    rebuilds = [json.loads(value) for value in wire.on(TOPIC_REBUILD_REQUESTED)]
    assert [r["correlation_id"] for r in rebuilds] == [
        str(c.correlation_id) for c in sent
    ]
    assert (
        len(wire.on(TOPIC_DEV_ROLL_HOLD)) == 2
    )  # the released commands were not held again


# ---- seam controls: a planted break turns the golden expectation red on both buses ----


async def test_control_the_unbroken_golden_expectations_hold(rig: Rig) -> None:
    await golden_hold_and_release(rig)


async def test_control_a_renamed_command_field_turns_the_hold_and_release_red(
    rig: Rig,
) -> None:
    with pytest.raises(AssertionError):
        await golden_hold_and_release(rig, rename=("git_ref", "ref"))


@pytest.mark.parametrize(
    ("field", "renamed"),
    [
        ("completed_at", "finished_at"),
        ("correlation_id", "job_id"),
        ("status", "state"),
    ],
)
async def test_control_a_renamed_completion_field_turns_the_completion_red(
    rig: Rig, field: str, renamed: str
) -> None:
    with pytest.raises(AssertionError):
        await golden_completion(rig, rename=(field, renamed))


async def test_control_a_bare_payload_on_the_hold_topic_is_not_held(rig: Rig) -> None:
    item = command(number=1)
    await rig.transport.send(
        HOLD_TOPIC, key=KEY, value=item.model_dump_json().encode(), headers={}
    )
    await rig.settle()
    assert await rig.on(HELD_TOPIC) == []
    assert rig.gate.store.load().held == ()
