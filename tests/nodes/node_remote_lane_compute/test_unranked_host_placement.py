# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""An unranked host (rank_free None) places below every ranked host (OMN-20669)."""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_remote_lane_compute.handlers.handler_remote_lane_placement import (
    HandlerRemoteLanePlacement,
)
from omnimarket.nodes.node_remote_lane_compute.models import (
    ModelRemoteLaneHostReading,
    ModelRemoteLanePlacementRequest,
)


def _reading(name: str, rank_free: float | None) -> ModelRemoteLaneHostReading:
    return ModelRemoteLaneHostReading(
        name=name,
        engines=("claude_sonnet", "codex"),
        lane_slots=2,
        lane_cap=4,
        rank_free=rank_free,
    )


@pytest.mark.unit
@pytest.mark.parametrize("engine", ["codex", "claude_sonnet"])
@pytest.mark.parametrize("ranked", [7.19, 0.0, -3.0])
def test_unranked_host_loses_to_any_ranked_host(engine: str, ranked: float) -> None:
    for readings in (
        (_reading("h101", None), _reading("h202", ranked)),
        (_reading("h202", ranked), _reading("h101", None)),
    ):
        result = HandlerRemoteLanePlacement().handle(
            ModelRemoteLanePlacementRequest(engine=engine, readings=readings)
        )
        assert result.host == "h202"


@pytest.mark.unit
def test_unranked_host_is_still_chosen_when_it_is_the_only_one() -> None:
    result = HandlerRemoteLanePlacement().handle(
        ModelRemoteLanePlacementRequest(
            engine="codex", readings=(_reading("h101", None),)
        )
    )
    assert result.host == "h101"
