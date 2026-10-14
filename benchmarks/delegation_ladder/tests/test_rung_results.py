# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The committed rung-benchmark results are internally consistent (OMN-20844).

These tests do not decide whether a rung is good. They check that what is
committed is what the report quotes: every default rung answered every task,
each class holds at least 25 tasks per rung, every recorded verdict is the one
the scorer re-derives from the saved response, and the committed table is the
table those records give.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks.delegation_ladder.run_ladder import RUNGS, load_bundles, rescore_dir
from benchmarks.delegation_ladder.summarise_results import (
    DIFFICULTY_ORDER,
    MINIMUM_TASKS_PER_CLASS,
    class_rung_table,
    class_separation,
    difficulty_tier,
    first_miss_tier,
    render_difficulty_table,
    render_table,
    tier_rung_table,
)

pytestmark = pytest.mark.unit

HERE = Path(__file__).resolve().parent.parent
RESULTS = HERE / "results" / "rungs" / "2026-10-14"
LADDER = HERE / "results" / "rungs" / "2026-10-14-ladder"
LADDER_EASY_MEDIUM = HERE / "results" / "rungs" / "2026-10-14-ladder-easy-medium"

# The classes this result set covers. The lab Qwen and both GLM rungs were also
# run over document, review and reasoning; those records are not committed here.
TIERED_CLASSES = ("test", "code_generation")


def _records() -> dict[tuple[str, str], dict[str, object]]:
    records: dict[tuple[str, str], dict[str, object]] = {}
    for path in sorted(RESULTS.glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                record = json.loads(line)
                records[(record["model_rung"], record["task_id"])] = record
    return records


def test_there_are_results_to_check() -> None:
    assert _records(), f"no records under {RESULTS}"


def test_every_default_rung_answered_every_task_of_the_classes_it_ran() -> None:
    bundles = load_bundles(None)
    records = _records()
    for name, rung in RUNGS.items():
        if not rung.in_default_set:
            continue
        got = {task for (model_rung, task) in records if model_rung == name}
        classes = {b.task_type for b in bundles if b.task_id in got}
        assert set(TIERED_CLASSES) <= classes, f"{name} did not run {TIERED_CLASSES}"
        expected = {b.task_id for b in bundles if b.task_type in classes}
        assert got == expected, (
            f"{name}: missing {sorted(expected - got)[:5]}, extra {sorted(got - expected)[:5]}"
        )


def test_every_class_holds_at_least_the_minimum_tasks_for_every_rung() -> None:
    rows = class_rung_table(list(_records().values()), list(RUNGS))
    assert set(TIERED_CLASSES) == {row["task_type"] for row in rows}
    for row in rows:
        assert row["tasks"] >= MINIMUM_TASKS_PER_CLASS, row
        assert row["meets_minimum_tasks"] is True


def test_no_default_rung_is_credited_with_an_answer_it_did_not_deliver() -> None:
    for record in _records().values():
        if record["accepted"]:
            assert record["delivered"] is True
            assert record["scorer_outcome"] == "pass"


def test_every_saved_response_rescores_to_the_recorded_verdict() -> None:
    assert rescore_dir(RESULTS) == 0


# One task per executing scorer: the cells the AC16 evidence rescores offline.
EXECUTING_CELLS = {"R4-01": "unit_test_execution", "R6-01": "patch_apply_and_test"}


def test_an_executing_task_of_each_scorer_rescores_on_every_rung() -> None:
    bundles = {b.task_id: b for b in load_bundles(list(EXECUTING_CELLS))}
    assert {t: b.scorer.value for t, b in bundles.items()} == EXECUTING_CELLS
    rungs = {r["model_rung"] for r in _records().values()}
    for task_id in EXECUTING_CELLS:
        assert {m for (m, t) in _records() if t == task_id} == rungs, task_id
    assert rescore_dir(RESULTS, only=list(EXECUTING_CELLS)) == 0


def test_the_committed_table_is_the_table_the_records_give() -> None:
    rows = class_rung_table(list(_records().values()), list(RUNGS))
    expected = render_table(rows, class_separation(rows))
    assert (RESULTS / "table.txt").read_text(encoding="utf-8") == expected


def test_every_record_names_the_code_and_host_that_produced_it() -> None:
    for record in _records().values():
        assert record.get("driver_sha"), record["task_id"]
        assert record.get("host"), record["task_id"]


def test_a_pinned_onex_row_that_the_ladder_moved_is_not_counted_for_its_rung() -> None:
    records = list(_records().values())
    moved = {(r["model_rung"], r["task_type"]): 0 for r in records}
    for record in records:
        if record.get("backend_pin_honoured") is False:
            moved[(record["model_rung"], record["task_type"])] += 1
    for row in class_rung_table(records, list(RUNGS)):
        assert row["pin_moved"] == moved[(row["model_rung"], row["task_type"])]
        accepted_in_records = sum(
            1
            for r in records
            if r["model_rung"] == row["model_rung"]
            and r["task_type"] == row["task_type"]
            and r["accepted"]
            and r.get("backend_pin_honoured") is not False
        )
        assert row["accepted"] == accepted_in_records


def _tiers() -> dict[str, str]:
    return {
        b.task_id: tier
        for b in load_bundles(None)
        if (tier := difficulty_tier(b.task_type, len(b.prompt))) is not None
    }


def test_every_tier_of_every_tiered_class_holds_eight_tasks_for_every_rung() -> None:
    rows = tier_rung_table(list(_records().values()), _tiers(), list(RUNGS))
    seen = {
        (r["task_type"], r["difficulty"], r["model_rung"]): r["tasks"] for r in rows
    }
    for name, rung in RUNGS.items():
        if not rung.in_default_set:
            continue
        for task_type in TIERED_CLASSES:
            for tier in DIFFICULTY_ORDER:
                assert seen.get((task_type, tier, name), 0) >= 8, (
                    name,
                    task_type,
                    tier,
                )


def test_the_committed_tier_table_is_the_table_the_records_give() -> None:
    rows = tier_rung_table(list(_records().values()), _tiers(), list(RUNGS))
    expected = render_difficulty_table(rows, first_miss_tier(rows))
    assert (RESULTS / "tiers.txt").read_text(encoding="utf-8") == expected


def _ladder_records(directory: Path = LADDER) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for path in sorted(directory.glob("*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.mark.parametrize(
    ("directory", "tiers"),
    [(LADDER, {"hard"}), (LADDER_EASY_MEDIUM, {"easy", "medium"})],
)
def test_the_unpinned_ladder_ran_each_task_of_its_tiers_once_and_traced_it(
    directory: Path, tiers: set[str]
) -> None:
    wanted = {task_id for task_id, tier in _tiers().items() if tier in tiers}
    records = _ladder_records(directory)
    assert sorted(r["task_id"] for r in records) == sorted(wanted)
    for record in records:
        trace = record["ladder"]
        assert isinstance(trace, dict)
        assert trace["escalations"] >= 0
        if trace["answered_by"] is None:
            assert record["accepted"] is False, record["task_id"]
        else:
            assert trace["answered_tier"], record["task_id"]


@pytest.mark.parametrize("directory", [LADDER, LADDER_EASY_MEDIUM])
def test_the_committed_ladder_summary_is_the_one_the_records_give(
    directory: Path,
) -> None:
    from benchmarks.delegation_ladder.summarise_results import render_ladder_table

    assert (directory / "ladder.txt").read_text(
        encoding="utf-8"
    ) == render_ladder_table(_ladder_records(directory))
