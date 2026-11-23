# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gate's release reaches the bus through the runtime's own result normalization (OMN-26501).

The tick handler the contract declares must hand the runtime the released commands in a shape the runtime
publishes with the multi-event (def-B fan-out) seam at its default, OFF. With the seam OFF the runtime drops
a bare ``Sequence[BaseModel]`` return (``output_events=[]``, status SUCCESS), so a gate that returned its
released commands as a list marked them released in its own state, published none of them, and held every later
command behind a roll the agent never saw until the completion deadline abandoned it (dev, 2026-11-23: rolls
bf8d7d4c-6dc0-4299-a493-bb2210979c02 released 02:26:31Z and 4b3ddc3b-f469-4806-8dca-d0d6136fcdb6 released
03:26:31Z, each logged as "DROPPING" by omnibase_infra.runtime.auto_wiring.fanout_seam).

These tests run the declared handler's real return through ``handler_wiring._normalize_handler_result``, the
function the runtime applies to every handler return.
"""

from __future__ import annotations

import importlib
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_infra.runtime.auto_wiring import handler_wiring
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick
from pydantic import BaseModel

from omnimarket.events.runtime_deployment import ModelDeployPublishCommand
from omnimarket.nodes.node_dev_roll_window_gate_effect.handlers import (
    FileDevRollGateStore,
    HandlerDevRollWindowHold,
)
from omnimarket.nodes.node_dev_roll_window_gate_effect.models import (
    ModelDevRollGateDecision,
    ModelDevRollGateRollRecorded,
)
from tests.unit.nodes.node_dev_roll_window_gate_effect.conftest import (
    DEADLINE,
    POLICY,
    WINDOW,
    Clock,
    Gate,
    command,
)

pytestmark = pytest.mark.unit

CONTRACT = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_dev_roll_window_gate_effect/contract.yaml"
)


@pytest.fixture(autouse=True)
def seam_at_its_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runtime ships with the fan-out seam OFF; prove the test runs in that state."""
    monkeypatch.delenv(handler_wiring.ENV_MULTI_EVENT_PUBLISH_SEAM, raising=False)
    assert handler_wiring.multi_event_seam_enabled() is False


def a_tick(gate: Gate) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=gate.clock.now,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=gate.clock.now,
        correlation_id=uuid4(),
        scheduler_id="test",
        tick_interval_ms=1000,
    )


def a_tick_at(now: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=now,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=now,
        correlation_id=uuid4(),
        scheduler_id="test",
        tick_interval_ms=1000,
    )


def declared_tick_handler_class() -> type:
    """The handler class the contract routes ``dev_roll_window.tick`` to."""
    routes = yaml.safe_load(CONTRACT.read_text(encoding="utf-8"))["handler_routing"][
        "handlers"
    ]
    (route,) = (r for r in routes if r["operation"] == "dev_roll_window.tick")
    return getattr(
        importlib.import_module(route["handler"]["module"]), route["handler"]["name"]
    )


def declared_tick_handler(gate: Gate, *, policy=POLICY) -> object:
    """The declared tick handler, built as the gate builds its own."""
    return declared_tick_handler_class()(gate.store, policy=policy, clock=gate.clock)


def published_by_the_runtime(result: object) -> list[BaseModel]:
    """What the runtime publishes for one handler return: the real normalization, the seam at its default."""
    dispatched = handler_wiring._normalize_handler_result(
        result, {"payload": {}}, "ModelRuntimeTick", EnumNodeKind.EFFECT
    )
    return [] if dispatched is None else list(dispatched.output_events)


def published_over_ticks(
    handler: object, gate: Gate, ticks: int = 6
) -> list[BaseModel]:
    """Everything the runtime publishes for ``ticks`` consecutive ticks of the declared handler."""
    out: list[BaseModel] = []
    for _ in range(ticks):
        out += published_by_the_runtime(handler.handle(a_tick(gate)))
    return out


def commands_in(published: list[BaseModel]) -> list[ModelDeployPublishCommand]:
    return [m for m in published if isinstance(m, ModelDeployPublishCommand)]


def test_control_a_bare_sequence_return_is_dropped_by_the_runtime_with_the_seam_off() -> (
    None
):
    """Positive control: the failure mode this file guards is real, not an artifact of the helper."""
    assert published_by_the_runtime([command(number=1)]) == []
    assert published_by_the_runtime(command(number=1)) != []


def test_the_declared_tick_handler_publishes_every_released_command_oldest_first(
    gate: Gate,
) -> None:
    held = [command(number=n, age_seconds=WINDOW - 60 * n) for n in (1, 2, 3)]
    for item in held:
        gate.hold.handle(item)
    gate.clock.advance(WINDOW)

    sent = commands_in(published_over_ticks(declared_tick_handler(gate), gate))

    assert [c.correlation_id for c in sent] == [c.correlation_id for c in held]
    assert all(c.roll_window_released for c in sent)
    state = gate.store.load()
    assert state.held == ()
    assert state.outbox == ()
    assert state.last_roll is not None


def test_each_tick_hands_the_runtime_one_command_and_the_outbox_survives_a_new_handler(
    gate: Gate,
) -> None:
    held = [command(number=n, age_seconds=WINDOW - 60 * n) for n in (1, 2, 3)]
    for item in held:
        gate.hold.handle(item)
    gate.clock.advance(WINDOW)

    first = published_by_the_runtime(declared_tick_handler(gate).handle(a_tick(gate)))
    assert [c.correlation_id for c in commands_in(first)] == [held[0].correlation_id]
    waiting = gate.store.load().outbox
    assert [
        c.correlation_id for c in waiting if isinstance(c, ModelDeployPublishCommand)
    ] == [c.correlation_id for c in held[1:]]

    restarted = declared_tick_handler(
        gate
    )  # a fresh handler over the same durable state
    rest = commands_in(published_over_ticks(restarted, gate))
    assert [c.correlation_id for c in rest] == [c.correlation_id for c in held[1:]]


def test_a_gate_switched_off_publishes_what_it_flushes(gate: Gate) -> None:
    held = [command(number=n, age_seconds=30 - 10 * n) for n in (1, 2)]  # oldest first
    for item in held:
        gate.hold.handle(item)
    off = POLICY.model_copy(update={"mode": "off"})

    sent = commands_in(
        published_over_ticks(declared_tick_handler(gate, policy=off), gate)
    )

    assert [c.correlation_id for c in sent] == [c.correlation_id for c in held]
    assert all(c.roll_window_released for c in sent)


def test_a_tick_that_only_waits_publishes_its_hold_decision_and_no_command(
    gate: Gate,
) -> None:
    gate.hold.handle(command(number=1, age_seconds=0))
    published = published_over_ticks(declared_tick_handler(gate), gate, ticks=2)
    assert commands_in(published) == []
    assert [m.action for m in published if isinstance(m, ModelDevRollGateDecision)] == [
        "hold",
        "hold",
    ]
    assert len(gate.store.load().held) == 1


def test_the_roll_now_decision_follows_the_commands_it_released(gate: Gate) -> None:
    for number in (1, 2):
        gate.hold.handle(command(number=number, age_seconds=WINDOW))
    gate.clock.advance(WINDOW)

    published = published_over_ticks(declared_tick_handler(gate), gate)

    assert [type(m) for m in published] == [
        ModelDeployPublishCommand,
        ModelDeployPublishCommand,
        ModelDevRollGateDecision,
    ]
    assert published[-1].action == "roll_now"
    assert published[-1].released_count == 2


def test_the_outbox_reads_back_from_the_file_store_as_the_events_it_held(
    tmp_path: Path,
) -> None:
    store = FileDevRollGateStore(tmp_path)
    clock = Clock()
    hold = HandlerDevRollWindowHold(store, policy=POLICY, clock=clock)
    handler = declared_tick_handler_class()(store, policy=POLICY, clock=clock)
    for number in (1, 2):
        hold.handle(command(number=number, age_seconds=WINDOW))
    clock.advance(WINDOW)
    handler.handle(a_tick_at(clock.now))

    kinds = [type(e) for e in FileDevRollGateStore(tmp_path).load().outbox]

    assert kinds == [ModelDeployPublishCommand, ModelDevRollGateDecision]


def test_an_abandoned_roll_with_nothing_held_is_published_as_the_record_it_is(
    gate: Gate,
) -> None:
    gate.hold.handle(command(number=1, age_seconds=WINDOW))
    gate.clock.advance(WINDOW)
    handler = declared_tick_handler(gate)
    assert commands_in(published_over_ticks(handler, gate))  # the roll is released
    gate.clock.advance(DEADLINE)  # no completion arrives

    published = published_over_ticks(handler, gate)

    (recorded,) = [m for m in published if isinstance(m, ModelDevRollGateRollRecorded)]
    assert recorded.outcome == "abandoned"
