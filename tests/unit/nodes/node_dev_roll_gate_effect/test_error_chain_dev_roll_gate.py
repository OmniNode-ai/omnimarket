# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain for node_dev_roll_gate_effect over its real contract on the in-memory bus.

An overlay that cannot be read, a state file that is broken, a payload that is not the
model, and a decision the gate never asked for: each is a typed answer or a dead letter,
and none of them loses or releases a held command.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from omnibase_infra.runtime.core_runtime.dlq_resolver import derive_canonical_dlq_topic

from omnimarket.nodes.node_dev_roll_gate_effect.handlers import (
    FileDevRollGateStore,
    HandlerDevRollGateHold,
)
from omnimarket.nodes.node_dev_roll_gate_effect.models import (
    EnumDevRollGateObservedReason,
    ModelDevRollGateObserved,
)
from tests.unit.nodes.node_dev_roll_gate_effect.chain_support import (
    FACTS_TOPIC,
    HOLD_TOPIC,
    KEY,
    OBSERVED_TOPIC,
    RELEASED_TOPIC,
    envelope,
    gate_chain,
    gate_handlers,
    on,
    pump,
    send_completed,
    send_hold,
)
from tests.unit.nodes.node_dev_roll_gate_effect.conftest import POLICY, command

pytestmark = pytest.mark.unit


def _observed(broker) -> list[ModelDevRollGateObserved]:
    return [
        ModelDevRollGateObserved.model_validate(m.payload)
        for m in on(broker, OBSERVED_TOPIC)
    ]


async def test_error_a_gate_with_no_overlay_answers_node_unavailable_and_holds_nothing(
    store, clock, tmp_path: Path
) -> None:
    handlers = gate_handlers(store, clock)
    handlers["HandlerDevRollGateHold"] = HandlerDevRollGateHold(
        store, environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)}, clock=clock
    )
    dispatch, transport, broker, _ = gate_chain(store, clock, handlers)
    await send_hold(transport, command())
    assert await pump(dispatch) == 1
    (answer,) = _observed(broker)
    assert answer.reason is EnumDevRollGateObservedReason.NODE_UNAVAILABLE
    assert on(broker, FACTS_TOPIC) == []
    assert store.load().held == ()


async def test_error_a_broken_state_file_is_unavailable_and_the_file_is_left_as_it_was(
    clock, tmp_path: Path
) -> None:
    state = tmp_path / "gate" / "gate-state.json"
    state.parent.mkdir()
    state.write_text("{broken", encoding="utf-8")
    broken = FileDevRollGateStore(tmp_path / "gate")
    handlers = {
        **gate_handlers(broken, clock),
        "HandlerDevRollGateHold": HandlerDevRollGateHold(
            broken, policy=POLICY, clock=clock
        ),
    }
    dispatch, transport, broker, _ = gate_chain(broken, clock, handlers)
    await send_hold(transport, command())
    await pump(dispatch)
    (answer,) = _observed(broker)
    assert answer.reason is EnumDevRollGateObservedReason.NODE_UNAVAILABLE
    assert state.read_text(encoding="utf-8") == "{broken"


async def test_error_a_payload_that_is_not_the_hold_model_is_dead_lettered_and_held_nothing(
    store, clock
) -> None:
    dispatch, transport, broker, _ = gate_chain(store, clock)
    held = command()
    bad = {"command": {**held.model_dump(mode="json"), "correlation_id": "not-a-uuid"}}
    await transport.send(HOLD_TOPIC, key=KEY, value=envelope(bad), headers={})
    await pump(dispatch)
    assert store.load().held == ()
    assert on(broker, FACTS_TOPIC) == []
    assert on(broker, RELEASED_TOPIC) == []
    assert len(list(broker.records(derive_canonical_dlq_topic(HOLD_TOPIC), 0))) == 1


async def test_error_a_completion_for_nothing_the_gate_released_is_not_its_own(
    store, clock
) -> None:
    dispatch, transport, broker, _ = gate_chain(store, clock)
    await send_completed(transport, uuid4())
    await pump(dispatch)
    (answer,) = _observed(broker)
    assert answer.reason is EnumDevRollGateObservedReason.NOT_OURS
    assert on(broker, RELEASED_TOPIC) == []


async def test_error_the_same_command_twice_is_held_once_and_answered_already_held(
    store, clock
) -> None:
    dispatch, transport, broker, _ = gate_chain(store, clock)
    held = command()
    await send_hold(transport, held)
    await send_hold(transport, held)
    await pump(dispatch)
    assert len(store.load().held) == 1
    assert [a.reason for a in _observed(broker)] == [
        EnumDevRollGateObservedReason.ALREADY_HELD
    ]
    assert len(on(broker, FACTS_TOPIC)) == 1
