# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26129 AC6: each candidate the 2026-11-20 smoke could not measure has a dated later row.

Two of the three 2026-11-20 cheap_frontier candidates were never measured: OpenRouter answered
429 when the key was registered for them, so no task ran. A dated later smoke records what
today's attempts honestly gave. For each of the two, a receipt dated after 2026-11-20 that stores
the same ten task prompts as 2026-11-20, each with its text, must hold either

* a measured row: the model, a whole-number accepted count within its ten tasks, an outcome in
  plain words, and a positive p50_seconds only when something was accepted (a zero count carries
  no p50, since a median of nothing would be invented), or
* a not-measured entry: the model, its reason with the UTC time of the attempt, and the number
  of tries.

The check stays red until such a receipt exists; the controls below prove each rule can fail.
"""

from __future__ import annotations

import copy
import hashlib
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


def _stored_prompts(receipt: dict[str, Any]) -> list[Any]:
    return list(receipt.get("task_prompts") or [])


def _the_first_days_ten(receipt: dict[str, Any], first_day: list[Any]) -> bool:
    """The receipt stores the ten task prompts of the first day, each with its text."""
    prompts = _stored_prompts(receipt)
    return (
        len(prompts) == 10
        and prompts == first_day
        and all(
            isinstance(entry, dict)
            and isinstance(entry.get("prompt"), str)
            and entry["prompt"].strip()
            for entry in prompts
        )
    )


def _later_row_problems(receipts: list[dict[str, Any]]) -> list[str]:
    """One problem per candidate with no valid dated later row after the first day."""
    first_day = next(
        (
            _stored_prompts(receipt)
            for receipt in receipts
            if receipt.get("measured_on") == FIRST_DAY
        ),
        [],
    )
    problems: list[str] = []
    for model in UNMEASURED_ON_FIRST_DAY:
        found = False
        for receipt in receipts:
            prompts = len(_stored_prompts(receipt))
            if str(receipt.get("measured_on") or "") <= FIRST_DAY:
                continue
            if not _the_first_days_ten(receipt, first_day):
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


def test_a_later_receipt_must_store_the_first_days_ten_prompts() -> None:
    """The 2026-11-24 receipt stores the 2026-11-20 prompts, and a changed prompt is refused."""
    first = _first_day()
    stored = next(r for r in _dated_receipts() if r.get("measured_on") == "2026-11-24")
    assert _the_first_days_ten(stored, _stored_prompts(first))
    emptied = _later_receipt()
    emptied["task_prompts"][0]["prompt"] = ""
    assert _later_row_problems([first, emptied])
    replaced = _later_receipt()
    replaced["task_prompts"][3]["prompt"] = "Reply with the word yes."
    assert _later_row_problems([first, replaced])


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
        ("a stored prompt emptied", lambda r: r["task_prompts"][0].update(prompt="")),
        (
            "a stored prompt not one of the first day's ten",
            lambda r: r["task_prompts"][3].update(prompt="Reply with the word yes."),
        ),
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


# --- The 2026-11-24 receipt's own values, pinned --------------------------------------------
# The structural rules above accept any well-formed row. These pin what the kept run records
# (the results.json of the five 2026-11-24 runs, kept outside the repository) actually hold, so
# changing a time, a try count or the plain-words outcome fails here. Free text is pinned by
# SHA-256; change the receipt and its digest together, on purpose.
RECEIPT_DAY = "2026-11-24"
GEMMA, LING = UNMEASURED_ON_FIRST_DAY
GEMMA_WALL_SECONDS = [
    124.637,
    73.056,
    73.438,
    74.221,
    72.875,
    73.45,
    83.054,
    62.537,
    54.291,
    72.845,
]
GEMMA_OUTCOME_SHA = "d53802a58d36af5eba4db9ae6f17b18fa1ac13428cf15e979b0a561a65448d22"
GEMMA_THEN_SHA = "d88b6829f6142d0632b8852b97f8b2f5405f4daed8283c0d5357457e6687dc61"
LING_REASON_SHA = "3244ccd4e5cbbcb7cfcfdae2b78f28c8f8c7e0a10f7f8031c506d6d5b6fa0fa9"


def _sha(text: Any) -> str:
    return hashlib.sha256(str(text).encode()).hexdigest()


def _pinned_problems(receipt: dict[str, Any]) -> list[str]:
    """One problem per pinned value of the 2026-11-24 receipt that differs from the kept runs."""
    problems: list[str] = []
    rows = [r for r in receipt.get("smokes") or [] if r.get("model") == GEMMA]
    entries = [e for e in receipt.get("not_measured") or [] if e.get("model") == LING]
    if len(rows) != 1 or len(entries) != 1:
        return ["the gemma row or the ling entry is missing or repeated"]
    gemma, ling = rows[0], entries[0]
    expected: list[tuple[str, Any, Any]] = [
        ("gemma accepted", gemma.get("accepted"), 0),
        ("gemma tasks", gemma.get("tasks"), 10),
        ("gemma tries_per_task", gemma.get("tries_per_task"), 1),
        (
            "gemma wall_seconds_per_task",
            gemma.get("wall_seconds_per_task"),
            GEMMA_WALL_SECONDS,
        ),
        ("gemma exit_code_per_task", gemma.get("exit_code_per_task"), [1] * 10),
        ("gemma outcome", _sha(gemma.get("outcome")), GEMMA_OUTCOME_SHA),
        ("gemma second pass", _sha(gemma.get("then")), GEMMA_THEN_SHA),
        ("ling tries", ling.get("tries"), 4),
        ("ling reason", _sha(ling.get("reason")), LING_REASON_SHA),
        (
            "ling first-pass partial accepted",
            (ling.get("first_pass_partial") or {}).get("tasks_accepted"),
            [8, 9],
        ),
        (
            "ling first-pass partial times",
            (ling.get("first_pass_partial") or {}).get("wall_seconds"),
            {"8": 62.343, "9": 11.84},
        ),
    ]
    problems.extend(
        f"{name}: {got!r} is not {want!r}"
        for name, got, want in expected
        if got != want
    )
    if "p50_seconds" in gemma or "p50_seconds" in ling:
        problems.append("a p50 is recorded where nothing was accepted")
    return problems


def _recorded() -> dict[str, Any]:
    return next(r for r in _dated_receipts() if r.get("measured_on") == RECEIPT_DAY)


def test_the_2026_11_24_receipt_holds_the_values_the_kept_runs_recorded() -> None:
    assert _pinned_problems(_recorded()) == []


@pytest.mark.parametrize(
    ("label", "spoil"),
    [
        (
            "a wall time changed",
            lambda r: r["smokes"][0]["wall_seconds_per_task"].__setitem__(3, 9.9),
        ),
        ("tries_per_task changed", lambda r: r["smokes"][0].update(tries_per_task=9)),
        ("gemma accepted changed", lambda r: r["smokes"][0].update(accepted=3)),
        (
            "exit codes changed",
            lambda r: r["smokes"][0].update(exit_code_per_task=[0] * 10),
        ),
        ("gemma outcome changed", lambda r: r["smokes"][0].update(outcome="ok")),
        ("gemma second pass changed", lambda r: r["smokes"][0].update(then="fine")),
        ("ling tries changed", lambda r: r["not_measured"][0].update(tries=2)),
        (
            "ling reason changed",
            lambda r: r["not_measured"][0].update(reason="429 became 200 at 12:00:00Z"),
        ),
        (
            "ling partial changed",
            lambda r: r["not_measured"][0]["first_pass_partial"].update(
                tasks_accepted=[8]
            ),
        ),
        ("a p50 added", lambda r: r["smokes"][0].update(p50_seconds=3.0)),
        ("the ling entry removed", lambda r: r["not_measured"].clear()),
    ],
)
def test_the_pinning_refuses_a_receipt_that_drifts_from_the_kept_runs(
    label: str, spoil: Any
) -> None:
    receipt = copy.deepcopy(_recorded())
    spoil(receipt)
    assert _pinned_problems(receipt), label
