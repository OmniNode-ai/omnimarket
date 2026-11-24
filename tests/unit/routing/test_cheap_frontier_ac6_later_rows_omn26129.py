# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26129 AC6: each candidate the 2026-11-20 smoke could not measure has a dated later row.

Two of the three 2026-11-20 cheap_frontier candidates were never measured: OpenRouter answered
429 when the key was registered for them, so no task ran. A dated later smoke records what
today's attempts honestly gave. For each of the two, a receipt dated after 2026-11-20 that stores
all ten task prompts must hold either

* a measured row: the model, a whole-number accepted count within its ten tasks, an outcome in
  plain words, and a positive p50_seconds only when something was accepted (a zero count carries
  no p50, since a median of nothing would be invented), or
* a not-measured entry: the model, its reason with the UTC time of the attempt, and the number
  of tries.

The check stays red until such a receipt exists; the controls below prove each rule can fail.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.unit

_RECEIPTS = Path(__file__).resolve().parents[2] / "fixtures" / "receipts"
FIRST_DAY = "2026-11-20"
UNMEASURED_ON_FIRST_DAY = (
    "google/gemma-4-31b-it:free",
    "inclusionai/ling-3.2-flash:free",
)
_UTC_TIME = re.compile(r"\d{2}:\d{2}(:\d{2})?Z")


def _dated_receipts(directory: Path = _RECEIPTS) -> list[dict[str, Any]]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("cheap_frontier_smokes_*.json"))
    ]


def _whole(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _positive(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


def _measured_row_ok(row: dict[str, Any], prompts: int) -> bool:
    accepted, tasks = row.get("accepted"), row.get("tasks")
    if not (_whole(accepted) and _whole(tasks) and tasks == prompts):
        return False
    if not 0 <= accepted <= tasks:
        return False
    if not (isinstance(row.get("outcome"), str) and row["outcome"].strip()):
        return False
    # Something accepted: a positive p50. Nothing accepted: no p50 at all.
    return (
        _positive(row.get("p50_seconds")) if accepted > 0 else "p50_seconds" not in row
    )


def _unmeasured_entry_ok(entry: dict[str, Any]) -> bool:
    reason = entry.get("reason")
    return (
        isinstance(reason, str)
        and bool(_UTC_TIME.search(reason))
        and _whole(entry.get("tries"))
        and entry["tries"] >= 1
    )


def _later_row_problems(receipts: list[dict[str, Any]]) -> list[str]:
    """One problem per candidate with no valid dated later row after the first day."""
    problems: list[str] = []
    for model in UNMEASURED_ON_FIRST_DAY:
        found = False
        for receipt in receipts:
            prompts = len(receipt.get("task_prompts") or [])
            if str(receipt.get("measured_on") or "") <= FIRST_DAY or prompts != 10:
                continue
            for row in receipt.get("smokes") or []:
                found = found or (
                    row.get("model") == model and _measured_row_ok(row, prompts)
                )
            for entry in receipt.get("not_measured") or []:
                found = found or (
                    entry.get("model") == model and _unmeasured_entry_ok(entry)
                )
        if not found:
            problems.append(f"{model} has no valid row dated after {FIRST_DAY}")
    return problems


def test_each_candidate_unmeasured_on_the_first_day_has_a_dated_later_row() -> None:
    assert _later_row_problems(_dated_receipts()) == []


def _first_day() -> dict[str, Any]:
    return next(r for r in _dated_receipts() if r.get("measured_on") == FIRST_DAY)


def _later_receipt() -> dict[str, Any]:
    """A later receipt that measured one candidate and could not measure the other."""
    later = copy.deepcopy(_first_day())
    later["measured_on"] = "2026-11-22"
    later["smokes"] = [
        {
            "model": UNMEASURED_ON_FIRST_DAY[0],
            "accepted": 8,
            "tasks": 10,
            "p50_seconds": 3.1,
            "outcome": "8 of 10 accepted",
        }
    ]
    later["not_measured"] = [
        {
            "model": UNMEASURED_ON_FIRST_DAY[1],
            "reason": "429 at 2026-11-22T10:00:00Z",
            "tries": 4,
        }
    ]
    return later


def test_the_check_passes_on_a_later_receipt_covering_both() -> None:
    assert _later_row_problems([_first_day(), _later_receipt()]) == []


def test_a_zero_accepted_row_without_a_p50_is_a_valid_measured_row() -> None:
    later = _later_receipt()
    later["smokes"][0].update(accepted=0, outcome="none accepted")
    later["smokes"][0].pop("p50_seconds")
    assert _later_row_problems([_first_day(), later]) == []


@pytest.mark.parametrize(
    ("label", "spoil"),
    [
        ("dated on the first day", lambda r: r.update(measured_on=FIRST_DAY)),
        ("prompts not stored", lambda r: r.update(task_prompts=[])),
        ("accepted not a number", lambda r: r["smokes"][0].update(accepted=None)),
        ("accepted above the tasks", lambda r: r["smokes"][0].update(accepted=11)),
        ("fewer tasks than prompts", lambda r: r["smokes"][0].update(tasks=9)),
        ("no outcome", lambda r: r["smokes"][0].pop("outcome")),
        ("accepted rows with no p50", lambda r: r["smokes"][0].pop("p50_seconds")),
        (
            "an invented p50 beside zero accepted",
            lambda r: r["smokes"][0].update(accepted=0),
        ),
        (
            "unmeasured reason with no time",
            lambda r: r["not_measured"][0].update(reason="rate limited"),
        ),
        ("unmeasured entry with no tries", lambda r: r["not_measured"][0].pop("tries")),
        ("one candidate missing", lambda r: r["not_measured"].pop()),
    ],
)
def test_the_check_refuses_a_later_row_that_does_not_record_the_attempt(
    label: str, spoil: Any
) -> None:
    # Judged beside the first-day receipt only, so a real later receipt in the
    # directory cannot satisfy the check for a spoiled one.
    later = _later_receipt()
    spoil(later)
    assert _later_row_problems([_first_day(), later]), label
