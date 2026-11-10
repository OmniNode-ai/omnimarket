# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's pure helpers and output equal the timer's, recorded by running the timer over the recorded ledger.

``tests/fixtures/friction_rollup_tick_parity.json`` was written by running the timer's own ``main`` (the unit
origin/main, friction_rollup_timer.py) over ``tests/fixtures/friction_rollup_tick/`` with a fixed ``--now``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from tests.nodes.node_friction_rollup_tick_effect.tick_support import (
    PARITY,
    observed,
    run_case,
    tick_diff,
    timer_row,
    without_additive,
)

pytestmark = pytest.mark.unit
CASES = PARITY["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: str(c["name"]))
def test_node_output_equals_the_timers(case: dict[str, Any], tmp_path: Path) -> None:
    for tick, result in run_case(tmp_path, case):
        diff = tick_diff(tick, observed(result, tmp_path / "state"))
        assert diff == [], f"{case['name']} @ {tick['now']}: {diff}"


@pytest.mark.parametrize("case", CASES, ids=lambda c: str(c["name"]))
def test_node_writes_the_timers_files_and_row(
    case: dict[str, Any], tmp_path: Path
) -> None:
    ran = run_case(tmp_path, case)
    for tick, result in ran:
        if tick["dry_run"]:
            assert result.written == ()
            continue
        stamp = tick["now"].replace("-", "").replace(":", "")
        state = tmp_path / "state"
        assert (
            without_additive((state / f"{stamp}.json").read_text(encoding="utf-8"))
            == tick["json_text"]
        )
        assert (state / f"{stamp}.md").read_text(encoding="utf-8") == tick[
            "markdown_text"
        ]
        if tick is ran[-1][0]:
            latest = (state / "latest.json").read_text(encoding="utf-8")
            assert latest == result.report_json_text
            assert without_additive(latest) == tick["json_text"]
            assert (state / "latest.md").read_text(encoding="utf-8") == tick[
                "markdown_text"
            ]
        assert timer_row(result.status_row, state) == tick["row"]


def test_recorded_fixture_exercises_trend_needs_ticket_and_missing_archive() -> None:
    by_name = {c["name"]: c for c in CASES}
    second = by_name["recorded-day-trend"]["ticks"][1]
    assert " | trend=vs 2026-09-26T06:00:00Z:" in second["row"]
    document = json.loads(second["json_text"])
    assert document["summary"]["trend"]["previous"]["path"] == "20260926T060000Z.json"
    assert document["summary"]["top_classes"]
    missing = json.loads(by_name["archive-missing"]["ticks"][0]["json_text"])
    assert missing["windows"]["24h"]["sources"][0]["status"] == "missing"
    assert by_name["dry-run"]["ticks"][0]["stdout"].count("\n") == 2
    assert any("flag=needs-ticket" in t["row"] for c in CASES for t in c["ticks"])


def test_planted_divergence_turns_the_comparison_red(tmp_path: Path) -> None:
    case = next(c for c in CASES if c["name"] == "recorded-day-trend")
    tick, result = run_case(tmp_path, case)[1]
    honest = observed(result, tmp_path / "state")
    assert tick_diff(tick, honest) == []
    # One cell of the status row changes: 16 friction rows become 17.
    cell = copy.deepcopy(honest)
    assert "rows 16," in cell["row"]
    cell["row"] = cell["row"].replace("rows 16,", "rows 17,", 1)
    assert tick_diff(tick, cell) != []
    assert tick_diff(tick, cell)[0].startswith("row:")
    # One value of the report JSON changes.
    document = json.loads(honest["json_text"])
    document["summary"]["top_classes"][0]["cost_minutes"] += 1
    changed = {
        **honest,
        "json_text": json.dumps(document, indent=2, ensure_ascii=False) + "\n",
    }
    assert any(line.startswith("json_text:") for line in tick_diff(tick, changed))
    # One word of the markdown and the exit code change.
    markdown = {
        **honest,
        "markdown_text": honest["markdown_text"].replace("Trend", "Trent", 1),
    }
    assert any(line.startswith("markdown_text:") for line in tick_diff(tick, markdown))
    assert tick_diff(tick, {**honest, "exit_code": 3}) != []
