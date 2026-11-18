# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Parity of the goal check with the retired script on 20 recorded ticks (OMN-23734, AC1).

``fixtures/recorded_goal_ticks.json`` holds 20 hourly ticks recorded by running the script's own
``goal_tick_fold`` (its HandlerGoalReport, with the controller's admission and receipt files read by the
script's own readers): the inputs of each tick and the GOAL line, findings and record the script produced.
The record each tick kept is the next tick's ``previous``, so the chain is the script's own.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_throughput_goal_check_compute.handlers.handler_throughput_goal_check import (
    GOAL_UNREAD_FIX,
    HandlerThroughputGoalCheck,
)
from omnimarket.nodes.node_throughput_goal_check_compute.models.model_throughput_goal_check import (
    ModelThroughputGoalCheckRequest,
)

_FIXTURE = Path(__file__).parent / "fixtures" / "recorded_goal_ticks.json"
_TICKS: list[dict[str, Any]] = json.loads(_FIXTURE.read_text(encoding="utf-8"))["ticks"]


def _request(tick: dict[str, Any]) -> ModelThroughputGoalCheckRequest:
    return ModelThroughputGoalCheckRequest.model_validate(
        {k: v for k, v in tick.items() if k != "expected"}
    )


def _findings(result: Any) -> list[list[str]]:
    return [[f.level, f.loop, f.why, f.fix] for f in result.findings]


def test_the_recording_holds_twenty_ticks() -> None:
    assert len(_TICKS) == 20


@pytest.mark.parametrize("index", range(20))
def test_goal_line_findings_and_record_match_the_script_parity(index: int) -> None:
    tick = _TICKS[index]
    result = HandlerThroughputGoalCheck().handle(_request(tick))
    assert result.goal_line == tick["expected"]["goal_line"]
    assert _findings(result) == tick["expected"]["findings"]
    assert result.record == tick["expected"]["record"]


def test_the_recording_covers_each_branch_of_the_check() -> None:
    lines = [t["expected"]["goal_line"] for t in _TICKS]
    assert any(line.startswith("GOAL unread | no goal_scope") for line in lines)
    assert any(line.startswith("GOAL unread | no PR watcher state") for line in lines)
    assert any("rate_ok=NO" in line for line in lines)
    assert any("rate_ok=yes" in line for line in lines)
    assert any("open_fell_6h=yes" in line for line in lines)
    assert any("open_fell_6h=NO" in line for line in lines)
    assert any(t["admissions_unread"] for t in _TICKS)
    assert any(t["pr_reasons"] is None for t in _TICKS if t["watcher_state"])
    assert any("flipped_back=0" not in line for line in lines)


def test_a_planted_change_to_one_input_turns_the_parity_red() -> None:
    """Falsifier: the same tick with one green pull request's check-run stamp moved 3 hours later no longer waits."""
    index = 1
    tick = copy.deepcopy(_TICKS[index])
    planted = False
    for record in tick["watcher_state"]["prs"].values():
        ci = record.get("ci") or {}
        if ci.get("verdict") == "GREEN":
            ci["detail"][0][2] = tick["now"]
            ci["detail"][1][2] = tick["now"]
            planted = True
            break
    assert planted
    result = HandlerThroughputGoalCheck().handle(_request(tick))
    assert result.goal_line != _TICKS[index]["expected"]["goal_line"]


def test_a_changed_scope_list_turns_the_parity_red() -> None:
    tick = copy.deepcopy(_TICKS[2])
    tick["scope"]["paused"] = []
    result = HandlerThroughputGoalCheck().handle(_request(tick))
    assert (
        result.goal_line != _TICKS[2]["expected"]["goal_line"]
        or _findings(result) != _TICKS[2]["expected"]["findings"]
    )


def test_the_same_tick_again_reads_nothing_changed() -> None:
    last = _TICKS[19]
    again = copy.deepcopy(last)
    again["previous"] = last["expected"]["record"]
    result = HandlerThroughputGoalCheck().handle(_request(again))
    assert result.changed is False
    assert result.goal_line.startswith("GOAL changed=no | ")
    assert result.goal_line.endswith("| nothing changed")


def test_an_unread_scope_is_one_unknown_finding_and_no_record() -> None:
    result = HandlerThroughputGoalCheck().handle(
        ModelThroughputGoalCheckRequest(
            now="2026-11-14T05:00:00Z",
            scope_unread="/x/overlay.yaml is unreadable: boom",
            watcher_state={"prs": {}},
        )
    )
    assert result.goal_line == "GOAL unread | /x/overlay.yaml is unreadable: boom"
    assert [(f.level, f.loop, f.fix) for f in result.findings] == [
        ("unknown", "goal-scope", GOAL_UNREAD_FIX)
    ]
    assert result.record is None


def test_an_unread_admission_record_is_named_and_the_lists_alone_decide() -> None:
    tick = copy.deepcopy(_TICKS[7])
    assert tick["admissions_unread"]
    result = HandlerThroughputGoalCheck().handle(_request(tick))
    assert [f.loop for f in result.findings][:2] == ["goal-scope", "goal-admissions"]
    assert result.findings[1].level == "unknown"
