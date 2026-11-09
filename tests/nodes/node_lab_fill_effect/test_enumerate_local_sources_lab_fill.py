# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The local enumerate adapters run real child processes (OMN-23250)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from omnimarket.nodes.node_lab_fill_effect.protocols import (
    LabFillPortError,
    LabFillSourceCutError,
)
from omnimarket.nodes.node_lab_fill_effect.protocols.local_lab_fill_sources import (
    LocalLabFillPlansCopy,
    LocalLabFillSourceReaders,
    parse_plans,
    parse_rulings,
)

PLANS_DOC = {
    "plans": [
        {
            "verdict": "STALLED",
            "priority": "P0",
            "next_action_kind": "ticket",
            "next_action_ticket": "OMN-9",
            "path": "a/p.md",
            "hours_idle": 7,
        },
        {
            "verdict": "STALLED",
            "priority": "P3",
            "next_action_kind": "ticket",
            "next_action_ticket": "OMN-8",
        },
        {
            "verdict": "STALLED",
            "priority": "P0",
            "next_action_kind": "wait",
            "next_action_ticket": "OMN-7",
        },
        {
            "verdict": "STALLED",
            "priority": "P0",
            "next_action_kind": "ticket",
            "next_action_ticket": None,
        },
        {
            "verdict": "ACTIVE",
            "priority": "P0",
            "next_action_kind": "ticket",
            "next_action_ticket": "OMN-6",
        },
    ]
}


def _tool(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return str(path)


def _copy(tmp_path: Path, tool: str, now: float | None = None) -> LocalLabFillPlansCopy:
    return LocalLabFillPlansCopy(
        copy_path=tmp_path / "copy" / "plans.json",
        tool_path=tool,
        tool_env={},
        priorities=("P0",),
        action_kinds=("ticket",),
        write_linear_snapshot=lambda path, _budget: path.write_text(
            "{}", encoding="utf-8"
        ),
        **({"now": lambda: now} if now is not None else {}),
    )


def test_parse_plans_keeps_stalled_priority_lane_work_and_counts_the_rest() -> None:
    listed = parse_plans(PLANS_DOC, ("P0",), ("ticket",))
    assert listed.found == (("OMN-9", "plan p.md, priority P0, idle 7 h"),)
    assert dict(listed.dropped) == {
        "not-priority": 1,
        "not-lane-work": 1,
        "no-ticket": 1,
    }


def test_parse_errors_name_the_missing_list() -> None:
    with pytest.raises(LabFillPortError, match="no plans list"):
        parse_plans({}, ("P0",), ("ticket",))
    with pytest.raises(LabFillPortError, match="no unmechanized list"):
        parse_rulings([])


def test_plans_rebuild_saves_a_copy_and_a_young_copy_needs_no_second_read(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "reads.log"
    tool = _tool(
        tmp_path,
        "plans_today.py",
        f"""
        import json, sys
        open({str(marker)!r}, "a").write("read\\n")
        print(json.dumps({PLANS_DOC!r}))
        """,
    )
    copy = _copy(tmp_path, tool)
    assert copy.age_min() is None
    listed = copy.rebuild(30.0)
    assert listed.found[0][0] == "OMN-9"
    age = copy.age_min()
    assert age is not None
    assert age < 1
    assert copy.load() == listed
    assert marker.read_text().count("read") == 1


def test_planted_slow_plans_tool_is_cut_by_its_budget(tmp_path: Path) -> None:
    tool = _tool(tmp_path, "plans_today.py", "import time\ntime.sleep(15)\n")
    with pytest.raises(LabFillSourceCutError, match="timed out"):
        _copy(tmp_path, tool).rebuild(0.5)


def test_rulings_tool_exit_1_is_an_answer_and_exit_2_is_a_failure(
    tmp_path: Path,
) -> None:
    doc = json.dumps(
        {"unmechanized": [{"ts": "T", "lane": "L", "mechanism_tickets": ["OMN-5"]}]}
    )
    ok = _tool(tmp_path, "r1.py", f"import sys\nprint({doc!r})\nsys.exit(1)\n")
    bad = _tool(
        tmp_path, "r2.py", "import sys\nsys.stderr.write('boom')\nsys.exit(2)\n"
    )
    assert LocalLabFillSourceReaders(rulings_tool_path=ok, tool_env={}).read_rulings(
        10
    ).found == (("OMN-5", "RULING T lane=L"),)
    with pytest.raises(LabFillPortError, match="exit 2 boom"):
        LocalLabFillSourceReaders(rulings_tool_path=bad, tool_env={}).read_rulings(10)


def test_unmoved_readers_refuse_by_name(tmp_path: Path) -> None:
    readers = LocalLabFillSourceReaders(rulings_tool_path="x", tool_env={})
    with pytest.raises(LabFillPortError, match="lane-resend reader is not wired"):
        readers.read_resends(1)
    with pytest.raises(LabFillPortError, match="source-tickets reader is not wired"):
        readers.read_tickets(["OMN-1"], 1)
