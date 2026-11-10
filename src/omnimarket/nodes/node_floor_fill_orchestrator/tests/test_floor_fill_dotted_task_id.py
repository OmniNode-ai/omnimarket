# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A task id with a dot in it: the fill's key charset accepts it and the minted lane name holds no dot."""

from __future__ import annotations

import pytest

from omnimarket.models.floor_fill.model_floor_fill_state import line_and_task
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EnumFloorFillCommand,
    EnumFloorFillStatus,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import (
    harness,
    overlay,
)
from omnimarket.nodes.node_floor_fill_reducer.handlers import lane_name
from omnimarket.nodes.node_remote_lane_start_orchestrator.tests.lane_start_fixtures import (
    request as start_request,
)

pytestmark = pytest.mark.unit

TICK = EnumFloorFillCommand.TICK


def test_dotted_task_ids_start_and_mint_dotless_lanes() -> None:
    """A tick over a line whose task ids hold dots starts each key and mints lanes without dots."""
    h = harness(
        lines={"2026-10-31-wave-plan": ["W0.1", "W0.2"]},
        config=overlay(max_fill_lanes_per_line=2),
    )
    answer = h.orchestrator().run(TICK)
    assert answer.status is EnumFloorFillStatus.OK
    assert answer.exit_code == 0
    assert {s.key for s in h.starter.starts} == {
        "floor:2026-10-31-wave-plan:W0.1",
        "floor:2026-10-31-wave-plan:W0.2",
    }
    assert all("." not in s.lane for s in h.starter.starts)


def test_line_and_task_accepts_a_dotted_task_id() -> None:
    """A floor key with a dot in the task id splits into its line id and task id."""
    assert line_and_task("floor:2026-10-31-wave-plan:W0.1") == (
        "2026-10-31-wave-plan",
        "W0.1",
    )


def test_lane_name_of_a_dotted_key_has_no_dot_and_tells_keys_apart() -> None:
    """The lane minted from a dotted key holds no dot, and the key hash tells it from its dotless twin."""
    dotted = lane_name("floor:a:W0.1", 1)
    dotless = lane_name("floor:a:W-1", 1)
    assert dotted != dotless
    assert "." not in dotted
    assert "." not in dotless


def test_line_and_task_still_refuses_a_separator_task_id() -> None:
    """A task id holding ``|``, ``=`` or ``/`` stays refused: it is a separator, not an id character."""
    for task_id in ("a|b", "a=b", "a/b"):
        with pytest.raises(ValueError, match="is not a floor key"):
            line_and_task(f"floor:alpha:{task_id}")


def test_seam_the_lane_start_request_accepts_the_dotted_key_and_the_minted_lane() -> (
    None
):
    """The lane start node's request model (the fill's next seam) accepts a dotted key and the lane minted from it."""
    key = "floor:2026-10-31-wave-plan:W0.1"
    started = start_request(lane=lane_name(key, 1), key=key)
    assert started.key == key
    assert started.lane == lane_name(key, 1)
