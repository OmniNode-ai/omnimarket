# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host with no route to what the brief reads takes no lane (OMN-22887).

The recorded shape, 2026-11-07: lanes placed on h106 could not do their work. h106 had no ssh key to h201 and no
route to the dev broker admin port, yet placement ranked it by free capacity alone and sent the brief. A brief that
reads h201 or the broker admin port now names ``route:h201`` and ``route:broker-admin:dev``; a host whose reading
does not hold the route takes no lane, and when only such hosts have room the answer is INPUT_ABSENT naming the
unreachable targets, so the send is refused before it is made.
"""

from __future__ import annotations

from typing import Any

from omnimarket.nodes.node_remote_lane_compute.handlers.handler_remote_lane_placement import (
    HandlerRemoteLanePlacement,
)
from omnimarket.nodes.node_remote_lane_compute.models import (
    EnumRemoteLaneOutcome,
    ModelRemoteLanePlacementRequest,
    ModelRemoteLanePlacementResult,
)

ROUTES = ("route:h201", "route:broker-admin:dev")


def _reading(name: str, rank: float, present: tuple[str, ...] | None) -> dict[str, Any]:
    reading: dict[str, Any] = {
        "name": name,
        "engines": ["claude_sonnet"],
        "lane_slots": 2,
        "lane_cap": 4,
        "rank_free": rank,
        "mem_avail_gb": 32.0,
    }
    if present is not None:
        reading["present_inputs"] = list(present)
    return reading


def _place(
    readings: list[dict[str, Any]], inputs: tuple[str, ...] = ROUTES, **over: Any
) -> ModelRemoteLanePlacementResult:
    payload: dict[str, Any] = {
        "engine": "claude_sonnet",
        "readings": readings,
        "required_inputs": list(inputs),
        **over,
    }
    return HandlerRemoteLanePlacement().handle(
        ModelRemoteLanePlacementRequest.model_validate(payload)
    )


def _verdicts(result: ModelRemoteLanePlacementResult) -> dict[str, str]:
    return {v.host: v.reason for v in result.verdicts}


def test_h106_with_no_route_to_h201_or_the_broker_admin_port_is_passed_over() -> None:
    """The recorded case: h106 has the most room but no route, so the host that reaches both takes the lane."""
    result = _place(
        [_reading("h106", 9.0, ()), _reading("h101", 3.0, ROUTES)],
    )
    assert result.outcome == EnumRemoteLaneOutcome.ADMITTED
    assert result.host == "h101"
    assert _verdicts(result)["h106"] == "no-route: h201, broker-admin:dev"


def test_a_host_reaching_only_one_target_is_refused_naming_the_other() -> None:
    result = _place([_reading("h106", 9.0, ROUTES[:1])])
    assert result.host is None
    assert result.outcome == EnumRemoteLaneOutcome.INPUT_ABSENT
    assert result.refusal == "h106 no-route: broker-admin:dev"


def test_only_unreachable_hosts_with_room_refuse_the_send() -> None:
    result = _place([_reading("h106", 9.0, ()), _reading("h105", 3.0, ())])
    assert result.host is None
    assert result.outcome == EnumRemoteLaneOutcome.INPUT_ABSENT
    assert result.refusal == "h106 no-route: h201, broker-admin:dev"


def test_a_host_not_checked_for_its_routes_is_kept_off() -> None:
    result = _place([_reading("h106", 9.0, None)])
    assert result.host is None
    assert result.outcome == EnumRemoteLaneOutcome.INPUT_ABSENT
    assert _verdicts(result)["h106"].startswith("input-unread: ")


def test_a_pinned_unreachable_host_is_refused_not_sent() -> None:
    result = _place(
        [_reading("h106", 9.0, ()), _reading("h101", 3.0, ROUTES)],
        pinned_host="h106",
    )
    assert result.host is None
    assert result.outcome == EnumRemoteLaneOutcome.INPUT_ABSENT
    assert result.refusal == "h106 no-route: h201, broker-admin:dev"


def test_a_missing_clone_and_a_missing_route_are_both_named() -> None:
    result = _place(
        [_reading("h106", 9.0, ())],
        inputs=("clone:omnimarket", "route:h201"),
    )
    assert _verdicts(result)["h106"] == "no-route: h201; input-absent: clone:omnimarket"


def test_a_brief_reading_nothing_remote_places_by_capacity_as_before() -> None:
    """Positive control: a brief naming no route ignores reach, so the roomiest host still takes it."""
    result = _place(
        [_reading("h106", 9.0, None), _reading("h101", 3.0, ROUTES)], inputs=()
    )
    assert result.outcome == EnumRemoteLaneOutcome.ADMITTED
    assert result.host == "h106"
