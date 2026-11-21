# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A tick that starts lanes stores its answer on every path out of the pass, so a started lane is never an unseen run.

The alarm check keeps the fill's runs by the stored answer's ``seq``. A tick that started a lane and then lost the
conditional write of its state used to leave no stored answer at all: the lane was sent and the run did not exist for the
check. ``kind == "started"`` is the literal the lane start records (``ModelFloorKeyResult``), and ``lanes_started`` counts it.

Golden: a completed tick stores ``lanes_started`` equal to the lanes it started.
Error: a tick whose state write is lost after the first start stores a FAILED answer that still counts the started lane.
Failing control: a tick that started nothing and lost its write stores no answer (nothing was sent that could be missed).
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EnumFloorFillCommand,
    EnumFloorFillStatus,
    ModelFloorFillStoredAnswer,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    ModelStateRead,
    ProtocolFloorStateStore,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import (
    Harness,
    harness,
    overlay,
)

pytestmark = pytest.mark.unit

TICK = EnumFloorFillCommand.TICK
SCOPE = "floor-fill-test"
ANSWER_KEY = SCOPE + ":answer"


class _LoseStateWrites:
    """A store that refuses every conditional write of the state row once ``lost()`` reads true."""

    def __init__(
        self, inner: ProtocolFloorStateStore, h: Harness, after_starts: int
    ) -> None:
        self.inner = inner
        self.h = h
        self.after_starts = after_starts

    def load(self, key: str) -> ModelStateRead:
        return self.inner.load(key)

    def compare_and_set(
        self, key: str, expected_version: int, new_version: int, payload: str
    ) -> bool:
        if key == SCOPE and len(self.h.starter.starts) >= self.after_starts:
            return False
        return self.inner.compare_and_set(key, expected_version, new_version, payload)


def _stored(h: Harness) -> ModelFloorFillStoredAnswer | None:
    read = h.store.load(ANSWER_KEY)
    if read.payload is None:
        return None
    return ModelFloorFillStoredAnswer.model_validate_json(read.payload)


def test_floor_fill_answer_a_completed_tick_that_starts_lanes_stores_how_many() -> None:
    h = harness(config=overlay(max_fill_lanes_per_line=1))
    answer = h.orchestrator().run(TICK)
    stored = _stored(h)
    assert stored is not None
    assert len(h.starter.starts) >= 1
    assert stored.lanes_started == len(h.starter.starts)
    assert stored.lanes_started == sum(
        1 for r in answer.key_results if r.kind == "started"
    )
    assert stored.free_slots == 20


def test_floor_fill_answer_a_tick_that_loses_its_state_write_after_a_start_still_stores_its_answer() -> (
    None
):
    h = harness(config=overlay(max_fill_lanes_per_line=1))
    h.store = _LoseStateWrites(h.store, h, after_starts=1)
    answer = h.orchestrator().run(TICK)
    assert answer.status is EnumFloorFillStatus.FAILED
    assert len(h.starter.starts) >= 1, "the lane was started before the write was lost"
    stored = _stored(h)
    assert stored is not None, (
        "a started lane left no stored answer: the run was unseen"
    )
    assert stored.lanes_started == len(h.starter.starts)
    assert stored.status is EnumFloorFillStatus.FAILED
    assert stored.seq == 1


def test_floor_fill_answer_a_tick_that_started_nothing_and_lost_its_write_stores_none() -> (
    None
):
    h = harness(config=overlay(max_fill_lanes_per_line=1))
    # Every state write is refused from the first: nothing is reserved, so nothing is started.
    h.store = _LoseStateWrites(h.store, h, after_starts=0)
    answer = h.orchestrator().run(TICK)
    assert len(h.starter.starts) == 0
    assert answer.status is EnumFloorFillStatus.FAILED
    assert _stored(h) is None
