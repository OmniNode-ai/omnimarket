# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24914: a run reads closed only over the full registered keys, arm by arm."""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    TOPIC_CODE_TRY,
    TOPIC_RUN_CLOSED,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_rows import (
    EnumEvalRunState,
)
from tests.nodes.node_projection_eval_results.support import (
    MODEL,
    MemoryStore,
    at,
    close_event,
    code_event,
    deliver,
    make_writer,
    try_row,
)

pytestmark = pytest.mark.unit
RUN = "00000000-0000-0000-0000-0000000000a1"
_SEQUENCES = (("t1", MODEL, "S"), ("t2", MODEL, "S"))
ARM_B = "golden_only"


def _states(store: MemoryStore) -> set[tuple[EnumEvalRunState, str]]:
    return {(r.run_state, r.void_reason) for r in store.rates}


async def _try(writer: Any, task: str, arm: str, minute: int) -> None:
    row = try_row(RUN, task, context_factor_subset=arm)
    await deliver(writer, TOPIC_CODE_TRY, code_event(RUN, row), at(minute))


async def test_a_two_arm_run_is_not_closed_while_the_second_arm_lacks_a_task() -> None:
    """A close that names no arm: the count reaches the sequences with one arm's tries and one of the other's."""
    store = MemoryStore()
    writer, _ = make_writer(store)
    await deliver(writer, TOPIC_RUN_CLOSED, close_event(RUN, _SEQUENCES), at(1))
    await _try(writer, "t1", "off", 2)
    await _try(writer, "t2", "off", 3)
    await _try(writer, "t1", ARM_B, 4)
    assert _states(store) == {(EnumEvalRunState.CLOSING, "")}
    await _try(writer, "t2", ARM_B, 5)
    assert _states(store) == {(EnumEvalRunState.CLOSED, "")}
    assert {(r.context_arm, r.n) for r in store.rates if r.size_band == "all"} == {
        ("off", 2),
        (ARM_B, 2),
    }


async def test_a_two_arm_run_with_a_foreign_task_in_one_arm_is_void() -> None:
    store = MemoryStore()
    writer, _ = make_writer(store)
    await deliver(writer, TOPIC_RUN_CLOSED, close_event(RUN, _SEQUENCES), at(1))
    await _try(writer, "t1", "off", 2)
    await _try(writer, "t2", "off", 3)
    await _try(writer, "other", ARM_B, 4)
    assert _states(store) == {(EnumEvalRunState.VOID, "sample_mismatch")}


def _two_arm_close() -> dict[str, Any]:
    event = close_event(RUN, _SEQUENCES)
    event["sequences"] = [
        {**seq, "context_arm": arm}
        for arm in ("off", ARM_B)
        for seq in event["sequences"]
    ]
    return event


async def test_a_close_that_registers_arms_is_compared_on_the_full_key() -> None:
    store = MemoryStore()
    writer, _ = make_writer(store)
    await deliver(writer, TOPIC_RUN_CLOSED, _two_arm_close(), at(1))
    await _try(writer, "t1", "off", 2)
    await _try(writer, "t2", "off", 3)
    # Four registered keys, two held: the second arm has none in yet.
    assert _states(store) == {(EnumEvalRunState.CLOSING, "")}
    await _try(writer, "t1", ARM_B, 4)
    await _try(writer, "t2", ARM_B, 5)
    assert _states(store) == {(EnumEvalRunState.CLOSED, "")}


async def test_a_full_key_close_with_item_count_four_and_one_arm_held_is_closing_not_closed() -> (
    None
):
    """The count-and-pair comparison read this closed: four held tries over two tasks, one arm twice."""
    store = MemoryStore()
    writer, _ = make_writer(store)
    event = _two_arm_close()
    event["item_count"] = 2  # a producer that counts tasks, not arm keys
    await deliver(writer, TOPIC_RUN_CLOSED, event, at(1))
    await _try(writer, "t1", "off", 2)
    await _try(writer, "t2", "off", 3)
    assert _states(store) == {(EnumEvalRunState.CLOSING, "")}
