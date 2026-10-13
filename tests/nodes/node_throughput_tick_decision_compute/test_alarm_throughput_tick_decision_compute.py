# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20851: an unplaced lane and a lasting venv divergence are ALARM verdicts, not notes.

A lane the ledger records as dispatched whose runner wrote no placement receipt within 10 minutes is
an ALARM, and is not counted as running; a cross-host dispatch-venv divergence older than 30 minutes
is an ALARM, a younger one a NOTE. Nothing counts a lane as running without a CLAIM that carries
`host=` and a run id. The ALARM verdict is typed, so the caller can print it, write it into the tick's
evidence file and exit non-zero.
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
NOW = "2026-10-10T03:30:00Z"
READING = "h201:cores=32,load1=4.0,free_gb=64.0,placed=0,cap=4,slots=4,login=claude"
HOST = {"name": "h201", "parses": {READING: {"parsed": True}}}


def run(**overrides: Any) -> Any:
    payload: dict[str, Any] = {"now": NOW, "launchctl": "not_loaded", **overrides}
    return HANDLER.handle(ModelThroughputTickRequest.model_validate(payload))


def alarm_lines(result: Any) -> list[str]:
    return [line for line in result.lines if line.startswith("ALARM ")]


def lane(name: str, minutes_ago: float, **extra: Any) -> dict[str, Any]:
    seconds = int(NOW[17:19]) + int(NOW[14:16]) * 60 + int(NOW[11:13]) * 3600
    at = seconds - int(minutes_ago * 60)
    stamp = f"2026-10-10T{at // 3600:02d}:{at % 3600 // 60:02d}:{at % 60:02d}Z"
    return {"lane": name, "dispatched_at": stamp, **extra}


def running_receipt(**extra: Any) -> dict[str, Any]:
    return {
        "started_at": "2026-10-10T03:29:00Z",
        "host": "h201",
        "status": "running",
        "pid_alive": True,
        "readings": [READING],
        **extra,
    }


# --- AC1: an unplaced lane is an ALARM after 10 minutes and is not counted as running -------------


def test_unplaced_lane_alarm_not_running() -> None:
    result = run(dispatched_lanes=[lane("fix-a", 11)])
    [line] = alarm_lines(result)
    assert line.startswith(
        "ALARM unplaced-lane:fix-a | dispatched 2026-10-10T03:19:00Z, 11 min ago with no placement receipt"
    ), line
    assert " | FIX: " in line
    assert "never report it as running" in line.split(" | FIX: ", 1)[1]
    [alarm] = result.alarms
    assert alarm.kind == "ALARM"
    assert alarm.loop == "unplaced-lane:fix-a"
    assert alarm.since == "2026-10-10T03:19:00Z"
    assert alarm.age_minutes == 11
    assert alarm.fix == line.split(" | FIX: ", 1)[1]
    # an ALARM is its own class: it is neither a MISSING nor an UNKNOWN finding
    assert "unplaced-lane:fix-a" not in result.missing + result.unknown
    assert result.exit_code == 1
    assert result.status_line.startswith("THROUGHPUT ALARM n=")
    assert "unplaced-lane:fix-a" in result.status_line
    assert "unplaced-lanes" in result.checked


def test_unplaced_lane_alarm_not_running_counts_no_slot_for_a_running_receipt_without_a_claim() -> (
    None
):
    lab = {"hosts": [HOST], "receipts": [running_receipt()]}
    unclaimed = run(lab_headroom=lab)
    [line] = [x for x in unclaimed.lines if x.startswith("MISSING lab-headroom:h201")]
    assert "running lanes 0 of cap 4, free slots 4" in line, line

    for claim in (
        {"claim_host": "h201"},  # no run id
        {"claim_run_id": "rlane-fix-a-1"},  # no host
    ):
        partial = run(
            lab_headroom={"hosts": [HOST], "receipts": [running_receipt(**claim)]}
        )
        [line] = [x for x in partial.lines if "lab-headroom:h201" in x]
        assert "running lanes 0 of cap 4" in line, (claim, line)

    claimed = run(
        lab_headroom={
            "hosts": [HOST],
            "receipts": [
                running_receipt(claim_host="h201", claim_run_id="rlane-fix-a-1")
            ],
        }
    )
    [line] = [x for x in claimed.lines if "lab-headroom:h201" in x]
    assert "running lanes 1 of cap 4, free slots 3" in line, line


def test_a_lane_younger_than_the_bound_is_a_note_and_the_bound_is_exclusive() -> None:
    for minutes in (3, 9.9, 10):
        result = run(dispatched_lanes=[lane("fix-a", minutes)])
        assert not alarm_lines(result), minutes
        assert result.alarms == [], minutes
        [line] = [x for x in result.lines if "unplaced-lane:fix-a" in x]
        assert line.startswith("NOTE unplaced-lane:fix-a"), (minutes, line)
    over = run(dispatched_lanes=[lane("fix-a", 10.1)])
    assert len(alarm_lines(over)) == 1


def test_the_unplaced_bound_is_a_request_field() -> None:
    assert alarm_lines(run(dispatched_lanes=[lane("fix-a", 11)])) != []
    result = run(dispatched_lanes=[lane("fix-a", 11)], unplaced_lane_alarm_minutes=20)
    assert alarm_lines(result) == []


def test_a_placed_lane_is_no_finding_and_no_dispatched_facts_skip_the_finding() -> None:
    placed = run(
        dispatched_lanes=[
            lane(
                "fix-a",
                45,
                placement_receipt=True,
                claim_host="h201",
                claim_run_id="rlane-fix-a-1",
            )
        ]
    )
    assert not [x for x in placed.lines if "unplaced-lane" in x]
    assert placed.alarms == []
    assert "unplaced-lanes" in placed.checked
    skipped = run()
    assert "unplaced-lanes" not in skipped.checked
    assert skipped.alarms == []


def test_each_unplaced_lane_is_its_own_alarm_oldest_first() -> None:
    result = run(
        dispatched_lanes=[lane("fix-b", 12), lane("fix-a", 40), lane("fix-c", 2)]
    )
    assert [a.loop for a in result.alarms] == [
        "unplaced-lane:fix-a",
        "unplaced-lane:fix-b",
    ]
    assert [x.split(" |", 1)[0] for x in alarm_lines(result)] == [
        "ALARM unplaced-lane:fix-a",
        "ALARM unplaced-lane:fix-b",
    ]


# --- AC2: venv divergence older than 30 minutes is an ALARM, younger is a NOTE --------------------


def divergence(minutes_ago: float, surface: str = "venv:omnimarket") -> dict[str, Any]:
    return {
        "surface": surface,
        "hosts": ["h101", "h201"],
        "diverged_since": lane("x", minutes_ago)["dispatched_at"],
        "detail": "omnimarket 0.9.4 on h101, 0.9.3 on h201",
    }


def test_venv_divergence_alarm_after_30m() -> None:
    result = run(venv_divergences=[divergence(31)])
    [line] = alarm_lines(result)
    assert line.startswith(
        "ALARM venv-divergence:venv:omnimarket | h101, h201 have differed since 2026-10-10T02:59:00Z "
        "(31 min, > 30)"
    ), line
    assert "omnimarket 0.9.4 on h101, 0.9.3 on h201" in line
    assert " | FIX: " in line
    [alarm] = result.alarms
    assert alarm.loop == "venv-divergence:venv:omnimarket"
    assert alarm.since == "2026-10-10T02:59:00Z"
    assert alarm.age_minutes == 31
    assert result.exit_code == 1
    assert result.status_line.startswith("THROUGHPUT ALARM n=")
    assert "venv-divergence" in result.checked


def test_venv_divergence_younger_than_30m_is_a_note_and_the_bound_is_exclusive() -> (
    None
):
    for minutes in (1, 29, 30):
        result = run(venv_divergences=[divergence(minutes)])
        assert result.alarms == [], minutes
        assert not alarm_lines(result), minutes
        [line] = [x for x in result.lines if "venv-divergence" in x]
        assert line.startswith("NOTE venv-divergence:venv:omnimarket"), (
            minutes,
            line,
        )


def test_the_venv_bound_is_a_request_field_and_no_facts_skip_the_finding() -> None:
    assert (
        run(venv_divergences=[divergence(31)], venv_divergence_alarm_minutes=60).alarms
        == []
    )
    assert run(venv_divergences=[]).alarms == []
    assert "venv-divergence" in run(venv_divergences=[]).checked
    assert "venv-divergence" not in run().checked


# --- the alarm record is typed, one per condition, and a calm tick carries none -------------------


def test_both_conditions_alarm_together_and_status_counts_both() -> None:
    result = run(
        dispatched_lanes=[lane("fix-a", 11)], venv_divergences=[divergence(45)]
    )
    assert [a.loop for a in result.alarms] == [
        "unplaced-lane:fix-a",
        "venv-divergence:venv:omnimarket",
    ]
    assert len(alarm_lines(result)) == 2
    assert result.model_dump(mode="json")["alarms"][0].keys() == {
        "kind",
        "loop",
        "since",
        "age_minutes",
        "why",
        "fix",
    }
