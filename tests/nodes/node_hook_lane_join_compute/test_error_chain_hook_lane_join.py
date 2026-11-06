# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Error chain: what the join refuses to guess, and the failing control (OMN-19998)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_hook_lane_join_compute.handlers import HandlerHookLaneJoin
from omnimarket.nodes.node_hook_lane_join_compute.models import (
    ModelHookLaneJoinAgent,
    ModelHookLaneJoinLane,
    ModelHookLaneJoinRequest,
)

T0 = datetime(2026, 11, 5, 23, 45, tzinfo=UTC)


def agent(
    agent_id: str, description: str | None, minutes: float = 1
) -> ModelHookLaneJoinAgent:
    return ModelHookLaneJoinAgent(
        session_id="s",
        agent_id=agent_id,
        description=description,
        first_event_at=T0 + timedelta(minutes=minutes),
    )


def join(
    agents: tuple[ModelHookLaneJoinAgent, ...], lanes: tuple[ModelHookLaneJoinLane, ...]
):
    return HandlerHookLaneJoin().handle(
        ModelHookLaneJoinRequest(agents=agents, lanes=lanes)
    )


LANE = ModelHookLaneJoinLane(name="lane-e-2330", claim_at=T0)


def test_failing_control_without_the_description_no_lane_is_named() -> None:
    """The executor-only shape the stamp join used to name: no description, so nothing is joined."""
    result = join((agent("executor", None),), (LANE,))
    assert result.joins == ()
    assert [u.reason for u in result.unjoined] == [
        "no agent description names the lane"
    ]


def test_two_agents_fitting_one_lane_stay_unjoined() -> None:
    result = join((agent("a", "lane-e"), agent("b", "lane-e", 2)), (LANE,))
    assert result.joins == ()
    assert "2 agents fit" in result.unjoined[0].reason


def test_two_lanes_fitting_one_agent_stay_unjoined() -> None:
    other = ModelHookLaneJoinLane(name="lane-e-2331", claim_at=T0)
    result = join((agent("a", "lane-e"),), (LANE, other))
    assert result.joins == ()
    assert {u.lane for u in result.unjoined} == {"lane-e-2330", "lane-e-2331"}


def test_a_name_that_only_contains_the_description_is_not_joined() -> None:
    for name in ("lane-e", "lane-e-extra", "other-lane-e-2330", "lane-e-23"):
        result = join(
            (agent("a", "lane-e"),), (ModelHookLaneJoinLane(name=name, claim_at=T0),)
        )
        assert result.joins == (), name


def test_an_agent_started_far_from_the_claim_is_not_joined() -> None:
    assert join((agent("a", "lane-e", 40),), (LANE,)).joins == ()


def test_a_lane_without_a_claim_is_left_unjoined_with_the_reason() -> None:
    result = join((agent("a", "lane-e"),), (ModelHookLaneJoinLane(name="lane-e-2330"),))
    assert result.unjoined[0].reason == "no CLAIM in the window"


def test_a_request_with_a_non_positive_window_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelHookLaneJoinRequest(name_window_seconds=0)


def test_a_request_with_an_unknown_field_is_refused() -> None:
    with pytest.raises(ValidationError):
        ModelHookLaneJoinRequest.model_validate({"agents": [], "surprise": 1})
