# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chains of the delegation tracker (OMN-23081): the two stages, from the scenario workflows' runs to the served tracker.

The golden chain reads nine green scenarios and writes a shippable tracker. Each error is a named injected break, and each
turns the reading: a failing scenario, a command that never answers, a missing source and a stale one all leave the tracker
not shippable, with the reason written in it.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_floor_delegation_effect.handlers.handler_floor_delegation import (
    HandlerFloorDelegation,
)
from omnimarket.nodes.node_floor_delegation_effect.models.model_floor_delegation import (
    ModelFloorDelegationRequest,
)

from .support import NOW, RT, FakeRunner, runs_answer, watcher_pr, write_home

pytestmark = pytest.mark.unit


def _answers(conclusion: str = "success") -> dict[str, tuple[int, str, str]]:
    return {
        d.workflow: (0, runs_answer(("completed", conclusion, 10)), "")
        for d in RT.scenarios
    }


def _chain(home: Path, answers: dict[str, tuple[int, str, str]]) -> dict:
    handler = HandlerFloorDelegation(RT, FakeRunner(answers), clock=lambda: NOW)
    first = handler.handle(
        ModelFloorDelegationRequest(stage="scenarios", home=str(home))
    )
    assert first.ok
    second = handler.handle(ModelFloorDelegationRequest(stage="build", home=str(home)))
    assert second.ok
    return json.loads((home / RT.output_relpath).read_text())


def test_golden_nine_green_scenarios_write_a_shippable_tracker(tmp_path: Path) -> None:
    home = write_home(
        tmp_path,
        prs={"omnimarket#7": watcher_pr(23161, number=7, verdict="RED")},
        lanes=[
            {
                "lane": "dlg-1",
                "ticket": "OMN-23172",
                "host": "h053",
                "liveness": "alive",
            }
        ],
    )
    doc = _chain(home, _answers())
    assert doc["shippable"] is True
    assert doc["counts"]["scenarios_passing"] == 9
    by = {t["id"]: t for t in doc["tickets"]}
    assert by["OMN-23161"]["group"] == "pr_open"
    assert by["OMN-23161"]["pr_ci"] == "red"
    assert by["OMN-23172"]["group"] == "lane_running"
    assert by["OMN-23172"]["host"] == "h053"
    assert all(s["ok"] for s in doc["sources"])


def test_error_a_failing_scenario_turns_the_tracker_not_shippable(
    tmp_path: Path,
) -> None:
    answers = _answers()
    answers[RT.scenarios[6].workflow] = (
        0,
        runs_answer(("completed", "failure", 10)),
        "",
    )
    doc = _chain(write_home(tmp_path), answers)
    assert doc["shippable"] is False
    assert doc["blocking"] == ["R7"]


def test_error_a_command_that_never_answers_is_unread_never_a_pass(
    tmp_path: Path,
) -> None:
    answers = _answers()
    answers[RT.scenarios[0].workflow] = (124, "", "timed out after 25 s")
    doc = _chain(write_home(tmp_path), answers)
    assert doc["shippable"] is False
    r1 = next(s for s in doc["scenarios"] if s["id"] == "R1")
    assert r1["state"] == "unread"
    assert "timed out" in r1["reason"]


def test_error_a_missing_ticket_mirror_is_named_in_the_sources(tmp_path: Path) -> None:
    home = write_home(tmp_path)
    (home / RT.ticket_mirror_relpath / "OMN-23170.json").unlink()
    doc = _chain(home, _answers())
    source = next(s for s in doc["sources"] if s["id"] == "mirror")
    assert not source["ok"]
    assert "OMN-23170" in source["detail"]


def test_error_a_stale_floor_record_leaves_no_ticket_reading_as_a_running_lane(
    tmp_path: Path,
) -> None:
    home = write_home(tmp_path, record_at=NOW - timedelta(hours=2))
    doc = _chain(home, _answers())
    assert next(s for s in doc["sources"] if s["id"] == "record")["ok"] is False
    assert all(t["group"] == "unread" for t in doc["tickets"])
