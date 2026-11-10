# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A host missing an input the brief needs takes no lane: the send is refused before it is made (OMN-19998).

The recorded shape: a lane's brief needs a map entry, a clone, a patch or a plan branch on its host. Placement
chose the host with the most free capacity, the brief was sent, and the lane found its input absent there. The
placement node now takes the inputs the brief needs (``required_inputs``) and, per host, the inputs the reader
found (``present_inputs``): a host lacking one, or not checked for them, takes no lane; when a host would take
the lane but for its inputs and none holding them does, the answer is INPUT_ABSENT naming the absent input.
"""

from __future__ import annotations

from typing import Any

from omnimarket.models.remote_lane_placement import (
    EnumRemoteLaneOutcome,
    ModelRemoteLanePlacementRequest,
    ModelRemoteLanePlacementResult,
)
from omnimarket.nodes.node_remote_lane_compute.handlers.handler_remote_lane_placement import (
    HandlerRemoteLanePlacement,
)

INPUTS = ("clone:omnimarket", "plan-branch:plan-5-3", "patch:/srv/lane/fix.patch")


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
    readings: list[dict[str, Any]], inputs: tuple[str, ...] = INPUTS, **over: Any
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


def test_the_best_ranked_host_without_the_plan_branch_is_passed_over() -> None:
    """The recorded case: the roomiest host lacks the plan branch, so the host holding every input takes it."""
    result = _place(
        [
            _reading("host-a", 9.0, INPUTS[:1] + INPUTS[2:]),
            _reading("host-b", 3.0, INPUTS),
        ]
    )
    assert result.outcome == EnumRemoteLaneOutcome.ADMITTED
    assert result.host == "host-b"
    assert _verdicts(result)["host-a"] == "input-absent: plan-branch:plan-5-3"


def test_no_host_holding_the_inputs_is_input_absent_not_a_send() -> None:
    result = _place([_reading("host-a", 9.0, INPUTS[:2]), _reading("host-b", 3.0, ())])
    assert result.host is None
    assert result.outcome == EnumRemoteLaneOutcome.INPUT_ABSENT
    assert result.refusal == "host-a input-absent: patch:/srv/lane/fix.patch"
    assert _verdicts(result)["host-b"] == "input-absent: " + ", ".join(INPUTS)


def test_an_unchecked_host_is_kept_off_a_lane_that_needs_inputs() -> None:
    result = _place([_reading("host-a", 9.0, None)])
    assert result.host is None
    assert result.outcome == EnumRemoteLaneOutcome.INPUT_ABSENT
    assert _verdicts(result)["host-a"].startswith("input-unread: ")


def test_a_pinned_host_missing_an_input_is_refused_not_sent() -> None:
    result = _place(
        [_reading("host-a", 9.0, INPUTS[1:]), _reading("host-b", 3.0, INPUTS)],
        pinned_host="host-a",
    )
    assert result.host is None
    assert result.outcome == EnumRemoteLaneOutcome.INPUT_ABSENT
    assert result.refusal == "host-a input-absent: clone:omnimarket"


def test_a_full_pool_stays_no_host_not_input_absent() -> None:
    full = _reading("host-a", 9.0, ())
    full["lane_slots"] = 0
    result = _place([full])
    assert result.outcome == EnumRemoteLaneOutcome.NO_HOST
    assert result.refusal is None


def test_a_lane_needing_no_input_places_as_before() -> None:
    """Positive control: an old producer's request (no required inputs) ignores the input readings."""
    result = _place(
        [_reading("host-a", 9.0, None), _reading("host-b", 3.0, ())], inputs=()
    )
    assert result.outcome == EnumRemoteLaneOutcome.ADMITTED
    assert result.host == "host-a"
