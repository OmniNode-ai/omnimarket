# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20840: the four things the tick printed as quiet notes, or not at all, on 2026-10-09.

At 23:30Z the tick read 10 merges in 60 minutes with 182 open and printed everything below as a NOTE:
three repositories under their landing floor behind a park lasting until the next morning, two lab
hosts it had no reading for, and nothing about six dispatched lanes whose receipts ended no-host
within a minute, or about the open count rising across runs. Each rule here turns one of them into a
finding with its FIX; the tick still dispatches nothing.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnimarket.nodes.node_throughput_tick_decision_compute.handlers.handler_throughput_tick_decision import (
    HandlerThroughputTickDecision,
)
from omnimarket.nodes.node_throughput_tick_decision_compute.models.model_throughput_tick_decision import (
    ModelThroughputTickRequest,
)

pytestmark = pytest.mark.unit

HANDLER = HandlerThroughputTickDecision()
NOW = "2026-10-09T23:30:00Z"
CAUSE = "cause:OmniNode-ai/omnibase_core:dead"
READING = "h201:cores=32,load1=4.0,free_gb=64.0,placed=1,cap=4,slots=3,login=claude"


def watcher(
    merged_minutes_ago: list[int], waiting: int, open_other: int = 0
) -> dict[str, Any]:
    prs: dict[str, Any] = {}
    for i, minutes in enumerate(merged_minutes_ago):
        hour, minute = divmod(23 * 60 + 30 - minutes, 60)
        prs[f"m{i}"] = {
            "facts": {
                "repo": "omnibase_core",
                "state": "MERGED",
                "merged_at": f"2026-10-09T{hour:02d}:{minute:02d}:00Z",
            }
        }
    for i in range(waiting):
        prs[f"w{i}"] = {
            "cls": "red",
            "facts": {"repo": "omnibase_core", "state": "OPEN", "author": "op"},
        }
    for i in range(open_other):
        prs[f"x{i}"] = {
            "cls": "red",
            "facts": {"repo": "r/other", "state": "OPEN", "author": "op"},
        }
    return {"last_tick": "2026-10-09T23:29:00Z", "operator": "op", "prs": prs}


def run(**overrides: Any) -> Any:
    payload: dict[str, Any] = {
        "now": NOW,
        "launchctl": "loaded",
        "launchctl_stdout": "{}",
        "ticks": [
            {"tick": 9, "ts": "2026-10-09T23:28:00Z", "mode": "act", "status": "OK"}
        ],
        "heartbeat_text": "2026-10-09T23:28:00Z\n",
        "watcher_path": "/w.json",
        "watcher_state": watcher([320], waiting=3),
        "floors_per_repo": {"omnibase_core": 0.5},
        **overrides,
    }
    return HANDLER.handle(ModelThroughputTickRequest.model_validate(payload))


def parked(until: str) -> dict[str, Any]:
    return {
        "causes": {
            CAUSE: {"repo": "omnibase_core", "members": [], "parked_until": until}
        }
    }


def floor_lines(result: Any) -> list[str]:
    return [line for line in result.lines if "floor:" in line]


# --- (a) a park that holds a stopped repository past N hours is MISSING ---------------------------


def test_a_long_park_over_a_repository_with_no_merges_is_missing_with_its_end_and_the_unblock_fix() -> (
    None
):
    result = run(state_json=parked("2026-10-10T08:00:00Z"))
    [line] = floor_lines(result)
    assert line.startswith(
        "MISSING floor:omnibase_core | omnibase_core merged 0 in 2h (0/h, floor 0.5/h) "
        "with 3 PRs waiting; last merge 5.3h ago; parked cause "
        + CAUSE
        + " until 2026-10-10T08:00:00Z, "
        "8.5h from now (> 2h)"
    ), line
    fix = line.split(" | FIX: ", 1)[1]
    assert "RULING 2026-10-09T04:10:56Z" in fix
    assert "fix lane" in fix
    assert CAUSE in fix
    assert "the tick dispatches nothing" in fix
    assert "floor:omnibase_core" in result.missing
    assert result.exit_code == 1


def test_the_park_bound_is_a_request_field() -> None:
    result = run(state_json=parked("2026-10-10T08:00:00Z"), park_max_hours=12)
    [line] = floor_lines(result)
    assert line.startswith("NOTE floor:omnibase_core")
    assert "parked cause" in line


@pytest.mark.parametrize(
    ("until", "merged"),
    [
        (
            "2026-10-10T00:30:00Z",
            [320],
        ),  # the park ends within 2 hours: the controller's own retry is near
        (
            "2026-10-10T08:00:00Z",
            [30, 320],
        ),  # one merge in the window: the repository is still landing
    ],
)
def test_a_short_park_or_a_landing_repository_stays_a_note(
    until: str, merged: list[int]
) -> None:
    result = run(
        state_json=parked(until),
        watcher_state=watcher(merged, waiting=3),
        floors_per_repo={"omnibase_core": 1.0},
    )
    [line] = floor_lines(result)
    assert line.startswith("NOTE floor:omnibase_core")
    assert "parked cause" in line


def test_a_live_lease_still_covers_a_floor() -> None:
    state = {
        "causes": {CAUSE: {"repo": "omnibase_core", "members": []}},
        "leases": [{"pr": CAUSE, "lease_id": "L7"}],
    }
    [line] = floor_lines(run(state_json=state))
    assert line.startswith("NOTE floor:omnibase_core")
    assert "owned by landing-L7" in line


# --- (b) an unread host is UNKNOWN, never a quiet NOTE ----------------------------------------------


def test_a_host_with_no_recent_reading_is_unknown_naming_the_read_that_refreshes_it() -> (
    None
):
    old = {
        "started_at": "2026-10-09T20:57:00Z",
        "host": "h201",
        "status": "done",
        "final": True,
        "readings": [READING],
    }
    result = run(
        lab_headroom={"hosts": [{"name": "h201"}, {"name": "h101"}], "receipts": [old]}
    )
    lab = [line for line in result.lines if "lab-headroom" in line]
    assert lab[0].startswith(
        "UNKNOWN lab-headroom:h201 | no placement reading in the last 15 min (newest 2026-10-09T20:57:00Z)"
    ), lab
    assert lab[1].startswith(
        "UNKNOWN lab-headroom:h101 | no placement reading in the last 15 min (newest none)"
    )
    for line in lab:
        assert "onex_lab_run.py --hosts" in line.split(" | FIX: ", 1)[1]
    assert result.unknown[-2:] == ["lab-headroom:h201", "lab-headroom:h101"]
    assert not any(
        line.startswith("NOTE lab-headroom") and "no recent placement" in line
        for line in result.lines
    )


# --- (c) a dispatched lane that never ran is MISSING until its brief runs ---------------------------


def receipt(lane: str, started: str, status: str, **extra: Any) -> dict[str, Any]:
    return {
        "lane": lane,
        "run_id": f"rlane-{lane}-1",
        "brief": f"/briefs/{lane}-brief.md",
        "path": f"/rl/{lane}/rlane-{lane}-1.json",
        "started_at": started,
        "status": status,
        "final": status not in ("placing", "preparing", "running"),
        **extra,
    }


def dispatch(*receipts: dict[str, Any], window: float | None = 3) -> Any:
    return run(
        lab_headroom={"hosts": [], "receipts": list(receipts)},
        **({"dispatch_window_hours": window} if window is not None else {}),
    )


@pytest.mark.parametrize(
    "status",
    ["no-host", "host-limited", "guard-memory-max", "guard-runtime-max", "failed"],
)
def test_a_recent_receipt_that_never_ran_is_missing_with_the_lane_and_its_reason(
    status: str,
) -> None:
    result = dispatch(
        receipt(
            "fix-a",
            "2026-10-09T21:00:00Z",
            status,
            reason="no host has a free lane slot",
        )
    )
    [line] = [x for x in result.lines if "dispatch:" in x]
    assert line.startswith(
        f"MISSING dispatch:fix-a | dispatched 2026-10-09T21:00:00Z, ended {status} (no host has a free lane slot); "
        "receipt /rl/fix-a/rlane-fix-a-1.json"
    ), line
    assert "dispatch:fix-a" in result.missing
    assert "the tick dispatches nothing" in line


def test_a_later_running_or_done_receipt_of_the_same_brief_clears_it() -> None:
    failed = receipt("fix-a", "2026-10-09T21:00:00Z", "no-host", reason="none free")
    for status in ("running", "done"):
        again = receipt(
            "fix-a-r2", "2026-10-09T22:00:00Z", status, brief=failed["brief"]
        )
        assert not [x for x in dispatch(failed, again).lines if "dispatch:" in x], (
            status
        )
    # a later receipt that is itself still placing, or failed again, clears nothing
    for status in ("placing", "no-host"):
        again = receipt("fix-a", "2026-10-09T22:00:00Z", status, reason="still none")
        lines = [x for x in dispatch(failed, again).lines if "dispatch:fix-a" in x]
        assert len(lines) == 1, (status, lines)
        assert lines[0].startswith("MISSING"), (status, lines)


def test_old_receipts_and_ordinary_ends_are_not_findings() -> None:
    result = dispatch(
        receipt("old", "2026-10-09T20:29:00Z", "no-host", reason="none free"),
        receipt("ok", "2026-10-09T21:00:00Z", "done"),
        receipt("live", "2026-10-09T23:00:00Z", "running"),
    )
    assert not [x for x in result.lines if "dispatch:" in x]
    assert "dispatches" in result.checked


def test_a_receipt_with_no_reason_names_its_readings() -> None:
    bare = receipt(
        "fix-b",
        "2026-10-09T21:00:00Z",
        "no-host",
        readings=["h201:UNREADABLE(ssh timeout)"],
    )
    [line] = [x for x in dispatch(bare).lines if "dispatch:" in x]
    assert "ended no-host (readings: h201:UNREADABLE(ssh timeout))" in line


def test_no_window_skips_the_finding_for_an_older_caller() -> None:
    result = dispatch(receipt("fix-a", "2026-10-09T21:00:00Z", "no-host"), window=None)
    assert "dispatches" not in result.checked
    assert not [x for x in result.lines if "dispatch:" in x]


# --- (d) an open count rising across the last three runs is MISSING ---------------------------------


def trend(history: list[tuple[str, int]], open_now: int) -> Any:
    return run(
        watcher_state=watcher([5, 10, 15, 20, 25], waiting=0, open_other=open_now),
        open_history=[{"at": at, "open": n} for at, n in history],
    )


def test_open_up_more_than_ten_percent_across_three_runs_is_missing() -> None:
    result = trend([("2026-10-09T23:10:00Z", 160), ("2026-10-09T23:20:00Z", 170)], 182)
    [line] = [x for x in result.lines if "open-trend" in x]
    assert line.startswith(
        "MISSING open-trend | open count 160 -> 170 -> 182 across the last three tick runs "
        "(since 2026-10-09T23:10:00Z, +13.8%, > 10%)"
    ), line
    assert "the tick dispatches nothing" in line
    assert result.open_count == 182
    assert "open-trend" in result.missing
    assert "open-trend" in result.checked


@pytest.mark.parametrize(
    "history",
    [
        [
            ("2026-10-09T23:10:00Z", 170),
            ("2026-10-09T23:20:00Z", 175),
        ],  # +7%: under the bar
        [("2026-10-09T23:20:00Z", 160)],  # two runs only
        [
            ("2026-10-09T19:00:00Z", 100),
            ("2026-10-09T23:20:00Z", 160),
        ],  # a run from another session
    ],
)
def test_a_flat_short_or_stale_history_is_a_note(
    history: list[tuple[str, int]],
) -> None:
    result = trend(history, 182)
    [line] = [x for x in result.lines if "open-trend" in x]
    assert line.startswith("NOTE open-trend"), line
    assert "open-trend" not in result.missing


def test_the_open_count_is_returned_for_the_caller_to_record_and_absent_when_unread() -> (
    None
):
    assert run().open_count == 3
    assert run(watcher_read_error="boom").open_count is None
    assert "open-trend" not in run().checked
