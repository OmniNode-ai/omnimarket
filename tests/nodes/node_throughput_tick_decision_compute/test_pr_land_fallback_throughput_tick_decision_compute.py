# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-headroom FIX falls back to pr-land lanes for idle slots (OMN-20864).

The tick reads the same typed PR-land facts as lab-fill selection and runs the same rule, so a free
slot with no session work names the parked, escalated or unowned-red PRs to land, and a free slot with
none of them says why.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.models.lab_fill import EnumLabFillPrLandFailure
from omnimarket.nodes.node_throughput_tick_decision_compute.handlers.handler_throughput_tick_decision import (
    HandlerThroughputTickDecision,
)
from omnimarket.nodes.node_throughput_tick_decision_compute.models.model_throughput_tick_decision import (
    ModelThroughputTickRequest,
)

pytestmark = pytest.mark.unit

HANDLER = HandlerThroughputTickDecision()
NOW = "2026-10-09T06:00:00Z"
READING = "h201:cores=32,load1=4.0,free_gb=64.0,placed=1,cap=4,slots=3,login=claude"
FULL = "h201:cores=32,load1=4.0,free_gb=64.0,placed=1,cap=4,slots=0,login=claude"
HEAD = "a" * 40


def _headroom(reading: str = READING) -> dict[str, Any]:
    return {
        "hosts": [{"name": "h201", "parses": {reading: {"parsed": True}}}],
        "receipts": [
            {
                "started_at": "2026-10-09T05:59:00Z",
                "host": "h201",
                "status": "running",
                "pid_alive": True,
                "readings": [reading],
            }
        ],
    }


def _pr_land(prs: list[dict[str, Any]], **holds: Any) -> dict[str, Any]:
    return {
        "prs": prs,
        "holds": {"source": "ledger", "read": True, "lines": [], **holds},
    }


def _run(pr_land: dict[str, Any] | None, reading: str = READING) -> Any:
    payload: dict[str, Any] = {
        "now": NOW,
        "launchctl": "not_loaded",
        "lab_headroom": _headroom(reading),
    }
    if pr_land is not None:
        payload["pr_land"] = pr_land
    return HANDLER.handle(ModelThroughputTickRequest.model_validate(payload))


def _lines(result: Any) -> list[str]:
    return [line for line in result.lines if "lab-headroom" in line]


def test_ac2_headroom_fix_with_idle_slots_and_a_parked_pr_plans_a_pr_land_dispatch() -> (
    None
):
    parked = {"pr": "repo_a#7", "head_sha": HEAD, "landing_state": "PARKED"}

    result = _run(_pr_land([parked]))

    assert result.pr_land is not None
    assert [(d.kind, d.pr) for d in result.pr_land.dispatch] == [
        ("pr-land", "repo_a#7")
    ]
    host_line = next(line for line in _lines(result) if "lab-headroom:h201" in line)
    assert host_line.startswith(
        "MISSING lab-headroom:h201 | running lanes 1 of cap 4, free slots 3"
    )
    assert (
        "with no session work, land these through the lab-fill effect as pr-land lanes: "
        "repo_a#7 (parked)" in host_line
    )


def test_ac3_headroom_with_idle_slots_and_no_fallback_pr_records_why() -> None:
    green = {"pr": "repo_a#7", "head_sha": HEAD, "ci_verdict": "GREEN"}

    result = _run(_pr_land([green]))

    assert result.pr_land is not None
    assert result.pr_land.dispatch == ()
    assert (
        "NOTE lab-headroom:pr-land | no parked, escalated or unowned-red open PR for 3 idle slots "
        "(1 open PRs read: handed-off=1)" in result.lines
    )


def test_an_unreadable_hold_source_is_unknown_and_dispatches_nothing() -> None:
    parked = {"pr": "repo_a#7", "head_sha": HEAD, "landing_state": "PARKED"}

    result = _run(_pr_land([parked], read=False, error="no ledger"))

    assert result.pr_land is not None
    assert result.pr_land.failure is EnumLabFillPrLandFailure.HOLD_SOURCE_UNREADABLE
    assert result.pr_land.dispatch == ()
    assert "lab-headroom:pr-land" in result.unknown
    assert not any("pr-land lanes:" in line for line in result.lines)


def test_a_full_lab_plans_no_pr_land_lane() -> None:
    parked = {"pr": "repo_a#7", "head_sha": HEAD, "landing_state": "PARKED"}

    result = _run(_pr_land([parked]), reading=FULL)

    assert result.pr_land is not None
    assert result.pr_land.dispatch == ()
    assert result.pr_land.idle_slots == 0


def test_without_pr_land_facts_the_headroom_fix_is_unchanged() -> None:
    result = _run(None)

    assert result.pr_land is None
    assert _lines(result) == [
        "MISSING lab-headroom:h201 | running lanes 1 of cap 4, free slots 3 | FIX: dispatch up to 3 "
        "lanes of the session's pillar work to h201 through the remote-lane runner; never pin to a host "
        "without headroom"
    ]
