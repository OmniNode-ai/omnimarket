# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the packaged contract resolves and the join names a lane's own agent (OMN-19998)."""

from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Any

import yaml

from omnimarket.nodes.node_hook_lane_join_compute.models import (
    ModelHookLaneJoinAgent,
    ModelHookLaneJoinLane,
    ModelHookLaneJoinRequest,
)

NAME = "node_hook_lane_join_compute"
T0 = datetime(2026, 11, 5, 23, 45, tzinfo=UTC)


def _contract() -> dict[str, Any]:
    return yaml.safe_load(
        files(f"omnimarket.nodes.{NAME}").joinpath("contract.yaml").read_text()
    )


def _handler() -> Any:
    entry = _contract()["handler_routing"]["handlers"][0]
    handler = entry["handler"]
    return getattr(importlib.import_module(handler["module"]), handler["name"])()


def test_contract_declares_the_bus_route_and_compute_shape() -> None:
    contract = _contract()
    assert contract["name"] == NAME
    assert contract["node_type"] == "compute"
    assert contract["descriptor"]["side_effects"] == []
    bus = contract["event_bus"]
    assert bus["subscribe_topics"] == [
        "onex.cmd.omnimarket.hook-lane-join-requested.v1"
    ]
    assert bus["publish_topics"] == ["onex.evt.omnimarket.hook-lane-join-decided.v1"]
    assert contract["terminal_event"] in bus["publish_topics"]
    assert {e["operation"] for e in contract["handler_routing"]["handlers"]} == {
        "join_hook_lanes"
    }


def test_golden_chain_names_the_lane_agent_and_counts_hosts() -> None:
    """The lane's own agent carries the description; the executor beneath it carries none and is not named."""
    request = ModelHookLaneJoinRequest(
        agents=(
            ModelHookLaneJoinAgent(
                session_id="s",
                agent_id="lane-agent",
                description="hook-telemetry-gaps",
                first_event_at=T0 + timedelta(minutes=1),
            ),
            ModelHookLaneJoinAgent(
                session_id="s",
                agent_id="executor",
                first_event_at=T0 + timedelta(minutes=2),
            ),
        ),
        lanes=(ModelHookLaneJoinLane(name="hook-telemetry-gaps-2345", claim_at=T0),),
        event_hosts=("mac", "mac", "hp-gen9", None),
    )
    result = _handler().handle(request)
    assert [(j.lane, j.agent_id) for j in result.joins] == [
        ("hook-telemetry-gaps-2345", "lane-agent")
    ]
    assert result.unjoined == ()
    assert [(h.host, h.events) for h in result.hosts] == [
        ("mac", 2),
        ("hp-gen9", 1),
        ("unstamped", 1),
    ]
