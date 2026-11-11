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


def _join_with_hosts(
    agents: tuple[ModelHookLaneJoinAgent, ...],
    event_hosts: tuple[str | None, ...],
    expected_hosts: tuple[str, ...],
):
    return HandlerHookLaneJoin().handle(
        ModelHookLaneJoinRequest(
            agents=agents,
            lanes=(LANE,),
            event_hosts=event_hosts,
            expected_hosts=expected_hosts,
        )
    )


def test_control_every_agent_fits_a_lane_and_every_host_reports() -> None:
    """Unbroken control: nothing is unmatched and no host is silent."""
    result = _join_with_hosts(
        (agent("a", "lane-e"),), ("mac", "hp-gen9"), ("mac", "hp-gen9")
    )
    assert [j.lane for j in result.joins] == ["lane-e-2330"]
    assert result.unmatched_agents == ()
    assert result.silent_hosts == ()


def test_injected_break_an_agent_that_fits_no_lane_is_named_not_dropped() -> None:
    result = _join_with_hosts(
        (agent("a", "lane-e"), agent("b", "lane-z", 2), agent("c", None, 3)),
        (),
        (),
    )
    assert [j.agent_id for j in result.joins] == ["a"]
    assert [(u.agent_id, u.description, u.reason) for u in result.unmatched_agents] == [
        ("b", "lane-z", "no lane fits the description and start time"),
        ("c", None, "the agent carries no description"),
    ]


def test_an_agent_started_outside_the_window_is_named_unmatched() -> None:
    result = _join_with_hosts((agent("a", "lane-e", 40),), (), ())
    assert [u.agent_id for u in result.unmatched_agents] == ["a"]


def test_injected_break_a_host_with_no_events_is_counted_at_zero_and_named() -> None:
    result = _join_with_hosts((), ("mac", "mac"), ("mac", "h202"))
    assert result.silent_hosts == ("h202",)
    assert [(h.host, h.events) for h in result.hosts] == [("mac", 2), ("h202", 0)]


def test_an_unexpected_host_that_reports_is_still_counted() -> None:
    result = _join_with_hosts((), ("stray",), ("mac",))
    assert result.silent_hosts == ("mac",)
    assert [(h.host, h.events) for h in result.hosts] == [("stray", 1), ("mac", 0)]
