# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The rescore of the 28-task benchmark covers every saved cell and lists its flips (OMN-20844).

Pure data checks over the committed files: the recorded side of each cell is read
back from merged-local.json and merged-cloud.json, and the flip list is compared
with a diff of recorded and rescored verdicts. No scorer runs here.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

RESULTS = Path(__file__).resolve().parent.parent / "results"
PATHS = ("local", "cloud")


def _cells() -> list[dict[str, Any]]:
    cells: list[dict[str, Any]] = json.loads(
        (RESULTS / "rescored-28-task-cells.json").read_text("utf-8")
    )
    return cells


def _saved() -> dict[tuple[str, str], dict[str, Any]]:
    saved: dict[tuple[str, str], dict[str, Any]] = {}
    for path in PATHS:
        merged = json.loads((RESULTS / f"merged-{path}.json").read_text("utf-8"))
        for record in merged["records"]:
            saved[(path, record["task_id"])] = record
    return saved


def test_every_saved_cell_is_rescored_once() -> None:
    cells = _cells()
    saved = _saved()
    on_disk = {
        (path, blob.stem)
        for path in PATHS
        for blob in (RESULTS / "responses" / path).glob("*.json")
    }
    keys = [(str(c["path"]), str(c["task_id"])) for c in cells]
    assert len(keys) == len(set(keys))
    assert len(cells) == len(saved) == len(on_disk) == 56
    assert set(keys) == set(saved) == on_disk


def test_recorded_side_is_what_the_old_results_hold() -> None:
    saved = _saved()
    for cell in _cells():
        record = saved[(str(cell["path"]), str(cell["task_id"]))]
        assert cell["recorded_outcome"] == record["score"]["outcome"]
        assert cell["recorded_score"] == record["score"]["score"]
        assert cell["rung"] == record["rung"]
        blob = json.loads(
            (
                RESULTS / "responses" / str(cell["path"]) / f"{cell['task_id']}.json"
            ).read_text("utf-8")
        )
        digest = hashlib.sha256(blob["response"].encode("utf-8")).hexdigest()
        assert digest == blob["sha256"] == record["response_sha256"]


def test_flip_list_is_the_diff_of_old_and_new_scores() -> None:
    cells = _cells()
    diff = sorted(
        f"{c['path']} {c['task_id']} {c['recorded_outcome']} -> {c['rescored_outcome']}"
        for c in cells
        if c["recorded_outcome"] != c["rescored_outcome"]
    )
    assert [bool(c["flipped"]) for c in cells] == [
        c["recorded_outcome"] != c["rescored_outcome"] for c in cells
    ]
    lines = (RESULTS / "rescored-28-task-flips.txt").read_text("utf-8").splitlines()
    listed = sorted(
        line for line in lines if " -> " in line and not line.startswith("Flips")
    )
    assert listed == diff
    assert lines[-1] == f"{len(diff)} flip(s) of {len(cells)} cells"


def test_flip_check_detects_a_changed_verdict() -> None:
    """Negative control: a cell whose rescored verdict is changed shows up as a flip."""
    cells = _cells()
    mutated = [dict(c) for c in cells]
    mutated[0]["rescored_outcome"] = (
        "fail" if mutated[0]["recorded_outcome"] == "pass" else "pass"
    )
    flips = [c for c in mutated if c["recorded_outcome"] != c["rescored_outcome"]]
    assert len(flips) == sum(1 for c in cells if c["flipped"]) + 1


@pytest.mark.parametrize("path", PATHS)
def test_rescored_table_matches_the_cells(path: str) -> None:
    table = (RESULTS / "rescored-28-task-table.txt").read_text("utf-8")
    cells = [c for c in _cells() if c["path"] == path]
    assert len(cells) == 28
    passed = sum(c["rescored_outcome"] == "pass" for c in cells)
    assert f"{passed}/28" in table
