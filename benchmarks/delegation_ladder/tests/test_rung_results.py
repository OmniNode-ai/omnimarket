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
    CLASS_ORDER,
    MINIMUM_TASKS_PER_CLASS,
    class_rung_table,
    class_separation,
    render_table,
)

pytestmark = pytest.mark.unit

HERE = Path(__file__).resolve().parent.parent
RESULTS = HERE / "results" / "rungs" / "2026-10-14"


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


def test_every_default_rung_answered_every_task_exactly_once() -> None:
    expected = {b.task_id for b in load_bundles(None)}
    records = _records()
    for name, rung in RUNGS.items():
        if not rung.in_default_set:
            continue
        got = {task for (model_rung, task) in records if model_rung == name}
        assert got == expected, (
            f"{name}: missing {sorted(expected - got)[:5]}, extra {sorted(got - expected)[:5]}"
        )


def test_every_class_holds_at_least_the_minimum_tasks_for_every_rung() -> None:
    rows = class_rung_table(list(_records().values()), list(RUNGS))
    assert {row["task_type"] for row in rows} == set(CLASS_ORDER)
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
