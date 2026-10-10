# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-20072: the S5 pilot's recorded evidence meets the plan's bar.

The pilot's verdicts were live observations, so the contract could not bind
AC4 to AC6 to anything a check can read. ``fixtures/omn20072_s5_pilot_evidence.json``
holds them as data: the per-PR verdicts with their merged heads and check-run
ids, the window totals, the constructed-case observations and the rollback
drill's readback. The bar is held here, not in the data, so the file cannot
lower its own threshold. The counted total is the number of recorded per-PR rows,
never a number the data declares on its own, and the drill is timed against the
first counted merge.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

DATA_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "omn20072_s5_pilot_evidence.json"
)

# Plan S5 proof, RULING 2026-10-02T15:53:06Z: 30 eligible PRs, no minimum days.
S5_MIN_COUNTED = 30
S5_CASES = frozenset(
    {
        "C1",
        "C2",
        "C4",
        "C5",
        "C8",
        "C10",
        "C12",
        "C13",
        "C14",
        "C15",
        "C16",
        "C17",
        "C18",
    }
)
COUNTING_OUTCOMES = frozenset(
    {"agree", "expected_difference", "negative_control_refused"}
)
PRE_PILOT_CONTEXT_COUNT = 35
_MERGED_HEAD = re.compile(r"[0-9a-f]{12}")
_TIMESTAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def _load() -> dict[str, Any]:
    loaded = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def count_violations(data: dict[str, Any]) -> list[str]:
    """Every way the recorded count falls short of the S5 bar (AC4)."""
    found: list[str] = []
    window = data["window"]
    rows = data["pilot_prs"]["rows"]
    counted = window["counted"]
    if counted < S5_MIN_COUNTED:
        found.append(f"counted {counted} is below the bar of {S5_MIN_COUNTED}")
    expected = (
        window["first_parent_merges"]
        - window["bot_version_bumps_excluded"]
        - len(window["not_counted"])
    )
    if counted != expected:
        found.append(f"counted {counted} is not merges less exclusions ({expected})")
    for name, value in window["bar_violations"].items():
        if value != 0:
            found.append(f"{name} is {value}, the bar is 0")
    if len(rows) != counted:
        found.append(f"{len(rows)} per-PR rows are not the counted total {counted}")
    if len(rows) < S5_MIN_COUNTED:
        found.append(
            f"{len(rows)} PRs carry a recorded check run, the bar is {S5_MIN_COUNTED}"
        )
    for row in rows:
        if row["outcome"] not in COUNTING_OUTCOMES:
            found.append(
                f"omnimarket#{row['pr']} outcome {row['outcome']} is a finding"
            )
        if not _MERGED_HEAD.fullmatch(row.get("merged_head", "")):
            found.append(f"omnimarket#{row['pr']} has no merged head")
        if not _TIMESTAMP.fullmatch(row.get("merged_at", "")):
            found.append(f"omnimarket#{row['pr']} has no merge time")
    prs = [row["pr"] for row in rows]
    if len(set(prs)) != len(prs):
        found.append("a PR repeats across per-PR rows")
    run_ids = [row["check_run"] for row in rows]
    if len(set(run_ids)) != len(run_ids):
        found.append("a check run id repeats across per-PR rows")
    if any(not isinstance(i, int) or i <= 0 for i in run_ids):
        found.append("a per-PR row has no check run id")
    return found


def case_violations(data: dict[str, Any]) -> list[str]:
    """Every plan case with no observation, or a malformed one (AC5)."""
    found: list[str] = []
    rows = data["cases"]["rows"]
    seen = [row["case"] for row in rows]
    if len(set(seen)) != len(seen):
        found.append("a case is listed twice")
    for missing in sorted(S5_CASES - set(seen)):
        found.append(f"{missing} has no observation")
    for row in rows:
        runs = row["check_runs"]
        if any(not isinstance(i, int) or i <= 0 for i in runs):
            found.append(f"{row['case']} has a malformed check run id")
        if not runs and not (row.get("ledger_status") and row.get("note")):
            found.append(f"{row['case']} cites neither a check run nor a ledger row")
    return found


def drill_violations(data: dict[str, Any]) -> list[str]:
    """Every way the drill's readback differs from the pre-pilot set (AC6)."""
    found: list[str] = []
    drill = data["drill"]
    contexts = drill["pre_pilot_contexts"]
    if len(contexts) != PRE_PILOT_CONTEXT_COUNT:
        found.append(
            f"{len(contexts)} pre-pilot contexts, expected {PRE_PILOT_CONTEXT_COUNT}"
        )
    if len(set(contexts)) != len(contexts):
        found.append("a pre-pilot context repeats")
    if drill["readback_added"] or drill["readback_removed"]:
        found.append(
            f"readback differs: added {drill['readback_added']}, "
            f"removed {drill['readback_removed']}"
        )
    first_counted = min(row["merged_at"] for row in data["pilot_prs"]["rows"])
    if not _TIMESTAMP.fullmatch(drill.get("performed_at", "")):
        found.append("the drill has no time")
    elif drill["performed_at"] >= first_counted:
        found.append(
            f"the drill at {drill['performed_at']} does not precede the first "
            f"counted PR, merged {first_counted}"
        )
    return found


def test_count_meets_the_s5_bar() -> None:
    assert count_violations(_load()) == []


def test_count_planted_disagreement_fails() -> None:
    data = _load()
    planted = copy.deepcopy(data)
    planted["pilot_prs"]["rows"][0]["outcome"] = "unclassified_difference"
    assert any("is a finding" in v for v in count_violations(planted))

    planted = copy.deepcopy(data)
    planted["window"]["bar_violations"]["accepted_negative_control"] = 1
    assert any("accepted_negative_control" in v for v in count_violations(planted))

    planted = copy.deepcopy(data)
    planted["window"]["counted"] = S5_MIN_COUNTED - 1
    assert any("below the bar" in v for v in count_violations(planted))

    planted = copy.deepcopy(data)
    planted["pilot_prs"]["rows"][1]["check_run"] = planted["pilot_prs"]["rows"][0][
        "check_run"
    ]
    assert any("repeats" in v for v in count_violations(planted))

    planted = copy.deepcopy(data)
    del planted["pilot_prs"]["rows"][-1]
    assert any("are not the counted total" in v for v in count_violations(planted))

    planted = copy.deepcopy(data)
    planted["pilot_prs"]["rows"] = planted["pilot_prs"]["rows"][: S5_MIN_COUNTED - 1]
    planted["window"]["counted"] = S5_MIN_COUNTED - 1
    assert any("the bar is" in v for v in count_violations(planted))

    planted = copy.deepcopy(data)
    planted["pilot_prs"]["rows"][1]["pr"] = planted["pilot_prs"]["rows"][0]["pr"]
    assert any("a PR repeats" in v for v in count_violations(planted))


def test_count_is_the_recorded_rows_not_a_declared_number() -> None:
    data = _load()
    assert len(data["pilot_prs"]["rows"]) == data["window"]["counted"]
    assert len(data["pilot_prs"]["rows"]) >= S5_MIN_COUNTED


def test_every_plan_case_has_an_observation() -> None:
    data = _load()
    assert case_violations(data) == []
    planted = copy.deepcopy(data)
    planted["cases"]["rows"] = [
        r for r in planted["cases"]["rows"] if r["case"] != "C5"
    ]
    assert case_violations(planted) == ["C5 has no observation"]


def test_drill_readback_equals_the_pre_pilot_context_set() -> None:
    data = _load()
    assert drill_violations(data) == []
    planted = copy.deepcopy(data)
    planted["drill"]["readback_removed"] = ["CI Summary"]
    assert any("readback differs" in v for v in drill_violations(planted))

    planted = copy.deepcopy(data)
    planted["drill"]["performed_at"] = max(
        row["merged_at"] for row in planted["pilot_prs"]["rows"]
    )
    assert any("does not precede" in v for v in drill_violations(planted))
