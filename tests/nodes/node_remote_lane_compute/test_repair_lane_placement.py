# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A lane that repairs a host never runs on that host, and reaches it over ssh (OMN-23389).

The recorded shape, 2026-11-09: the first cleanup lane for a full host was pinned to that host, which refused
admission, and the lane never ran. The placement node now takes the host a repair lane repairs
(``repair_target``) and, per reading, the hosts that reading reached over ssh (``ssh_reach``): the target takes
no repair lane whatever its admission reading says, a pin to the target names the target and not the run host,
and the lane runs on a host that reached the target over ssh.
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

REFUSAL = "DISK-FREE-UNDER-BAR: 4.08 GB free < 5 GB"


def _reading(
    name: str,
    rank: float,
    *,
    reach: tuple[str, ...] | None = ("full-host",),
    refusal: str | None = None,
) -> dict[str, Any]:
    reading: dict[str, Any] = {
        "name": name,
        "engines": ["claude_sonnet"],
        "lane_slots": 0 if refusal else 2,
        "lane_cap": 4,
        "rank_free": rank,
        "mem_avail_gb": 32.0,
        "lane_admission_refusal": refusal,
    }
    if reach is not None:
        reading["ssh_reach"] = list(reach)
    return reading


def _place(
    readings: list[dict[str, Any]], **over: Any
) -> ModelRemoteLanePlacementResult:
    payload: dict[str, Any] = {"engine": "claude_sonnet", "readings": readings, **over}
    return HandlerRemoteLanePlacement().handle(
        ModelRemoteLanePlacementRequest.model_validate(payload)
    )


def _verdicts(result: ModelRemoteLanePlacementResult) -> dict[str, str]:
    return {v.host: v.reason for v in result.verdicts}


def test_control_without_a_repair_target_the_refusing_pin_is_no_host() -> None:
    """The failing control: an ordinary lane pinned to the refusing host is refused there and goes nowhere."""
    result = _place(
        [
            _reading("full-host", 1.0, refusal=REFUSAL),
            _reading("host-b", 9.0),
        ],
        pinned_host="full-host",
    )
    assert result.host is None
    assert result.outcome == EnumRemoteLaneOutcome.NO_HOST
    assert _verdicts(result)["full-host"] == f"admission: {REFUSAL}"


def test_a_repair_brief_pinned_to_a_refusing_host_is_placed_on_another_host() -> None:
    """AC1 falsifier: the repair brief pinned to the refusing host is not admitted there."""
    result = _place(
        [
            _reading("full-host", 1.0, refusal=REFUSAL),
            _reading("host-b", 9.0),
        ],
        pinned_host="full-host",
        repair_target="full-host",
    )
    assert result.outcome == EnumRemoteLaneOutcome.ADMITTED
    assert result.host == "host-b"
    assert _verdicts(result)["full-host"].startswith("repair-target:")
    assert result.repair_pin_refused is not None
    assert "pinned to full-host" in result.repair_pin_refused
    assert REFUSAL in result.repair_pin_refused
    assert "placed on host-b" in result.repair_pin_refused


def test_a_repair_lane_never_runs_on_a_target_that_would_admit_it() -> None:
    """The target is off the pick even with free slots and the best rank: it is the host being repaired."""
    result = _place(
        [_reading("full-host", 99.0), _reading("host-b", 1.0)],
        repair_target="full-host",
    )
    assert result.host == "host-b"
    assert _verdicts(result)["full-host"].startswith("repair-target:")
    assert result.repair_pin_refused is None


def test_the_repair_lane_runs_only_where_the_target_was_reached_over_ssh() -> None:
    result = _place(
        [
            _reading("full-host", 1.0, refusal=REFUSAL),
            _reading("host-b", 9.0, reach=()),
            _reading("host-c", 3.0, reach=("full-host",)),
            _reading("host-d", 8.0, reach=None),
        ],
        repair_target="full-host",
    )
    assert result.host == "host-c"
    verdicts = _verdicts(result)
    assert verdicts["host-b"] == "no-ssh-to-full-host"
    assert verdicts["host-d"] == "no-ssh-to-full-host"


def test_error_chain_no_host_reaches_the_target_is_no_host_naming_ssh() -> None:
    result = _place(
        [
            _reading("full-host", 1.0, refusal=REFUSAL),
            _reading("host-b", 9.0, reach=()),
        ],
        pinned_host="full-host",
        repair_target="full-host",
    )
    assert result.host is None
    assert result.outcome == EnumRemoteLaneOutcome.NO_HOST
    assert result.repair_pin_refused is None
    assert _verdicts(result)["host-b"] == "no-ssh-to-full-host"


def test_a_pin_to_another_host_still_refuses_when_that_host_refuses_admission() -> None:
    """A repair lane pinned to a refusing host that is not its target is refused there with the admission reason."""
    result = _place(
        [
            _reading("full-host", 1.0, refusal=REFUSAL),
            _reading("host-b", 9.0, refusal="CPU-IDLE-UNDER-BAR"),
            _reading("host-c", 3.0),
        ],
        pinned_host="host-b",
        repair_target="full-host",
    )
    assert result.host is None
    assert _verdicts(result)["host-b"] == "admission: CPU-IDLE-UNDER-BAR"


def test_golden_chain_an_ordinary_lane_is_placed_as_before() -> None:
    result = _place(
        [_reading("host-a", 1.0, reach=None), _reading("host-b", 9.0, reach=None)]
    )
    assert result.host == "host-b"
    assert result.repair_pin_refused is None
