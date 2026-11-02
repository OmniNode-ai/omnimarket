# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""CM.11 AC2: repeat agreement by pairwise Jaccard, UNSTABLE flagged, matched removal."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_compaction_continuation_replay_effect.handlers.handler_repeat_stability import (
    HandlerRepeatStability,
)
from omnimarket.nodes.node_compaction_continuation_replay_effect.handlers.stability import (
    jaccard,
)
from omnimarket.nodes.node_compaction_continuation_replay_effect.models import (
    EnumStabilityStatus,
    ModelStabilityCell,
    ModelStabilityRequest,
    ModelStabilityResult,
)

pytestmark = pytest.mark.unit

SAME = ["gh.pr.view|OMN-1", "ledger_append|OMN-2", "read_file|"]


def _cell(b: str, arm: str, repeats: list[list[str]]) -> ModelStabilityCell:
    return ModelStabilityCell(boundary_id=b, arm=arm, repeats=repeats)


def _run(
    cells: list[ModelStabilityCell], tol_file: Path | None = None
) -> ModelStabilityResult:
    return HandlerRepeatStability().handle(
        ModelStabilityRequest(
            correlation_id="c",
            cells=cells,
            tolerance_file=str(tol_file) if tol_file else None,
        )
    )


def test_jaccard_ignores_order_and_duplicates() -> None:
    assert jaccard(["a", "b"], ["b", "a", "a"]) == 1.0
    assert jaccard(["a", "b"], ["b", "c"]) == pytest.approx(1 / 3)
    assert jaccard([], []) == 1.0


def test_unstable_flagged_below_tolerance() -> None:
    drift = [SAME, SAME, ["gh.pr.merge|OMN-9"]]
    res = _run([_cell("b1", "stock", drift), _cell("b1", "mod", [SAME] * 3)])
    grade = {g.arm: g for g in res.cells}
    assert grade["stock"].status == EnumStabilityStatus.UNSTABLE
    assert grade["stock"].min_pairwise_jaccard == 0.0
    assert grade["mod"].status == EnumStabilityStatus.STABLE


def test_unstable_flagged_at_tolerance_is_stable_and_rate_reported() -> None:
    five = ["a", "b", "c", "d", "e"]
    at = [
        five,
        five,
        ["a", "b", "c", "d"],
    ]  # jaccard 0.8 exactly: at the tolerance, stable
    res = _run([_cell("b1", "stock", at), _cell("b1", "mod", at)])
    assert all(g.status == EnumStabilityStatus.STABLE for g in res.cells)
    res2 = _run(
        [
            _cell("b1", "stock", [five, five, ["a", "b", "c"]]),
            _cell("b1", "mod", [five] * 3),
        ]
    )
    assert res2.unstable_rate == 0.5
    assert "UNSTABLE rate 0.500" in res2.report
    assert res2.unstable_rate_by_arm == {"mod": 0.0, "stock": 1.0}


def test_matched_removal_removes_the_other_arms_cell() -> None:
    res = _run(
        [
            _cell("b1", "stock", [SAME, SAME, ["x"]]),
            _cell("b1", "mod", [SAME] * 3),
            _cell("b2", "stock", [SAME] * 3),
            _cell("b2", "mod", [SAME] * 3),
        ]
    )
    by = {(g.boundary_id, g.arm): g for g in res.cells}
    assert not by[("b1", "stock")].retained
    assert by[("b1", "stock")].removal_reason == "UNSTABLE_CELL"
    assert not by[("b1", "mod")].retained
    assert by[("b1", "mod")].removal_reason == "MATCHED_REMOVAL"
    assert by[("b2", "stock")].retained
    assert by[("b2", "mod")].retained
    assert res.removed_boundaries == ["b1"]
    assert res.exclusion_rate_by_arm == {"mod": 0.5, "stock": 0.5}


def test_matched_removal_orphan_and_short_repeats() -> None:
    res = _run(
        [
            _cell("b1", "stock", [SAME] * 3),  # no mod cell: cannot be matched
            _cell("b2", "stock", [SAME] * 3),
            _cell("b2", "mod", [SAME, SAME]),  # two repeats: UNAVAILABLE, never stable
        ]
    )
    by = {(g.boundary_id, g.arm): g for g in res.cells}
    assert by[("b1", "stock")].removal_reason == "ORPHAN_CELL"
    assert by[("b2", "mod")].status == EnumStabilityStatus.UNAVAILABLE
    assert by[("b2", "stock")].removal_reason == "MATCHED_REMOVAL"
    assert not any(g.retained for g in res.cells)


def test_tolerance_is_read_from_the_data_file(tmp_path: Path) -> None:
    data = tmp_path / "tol.yaml"
    data.write_text("jaccard_tolerance: 0.2\nmin_repeats: 3\n")
    cells = [
        _cell("b1", "stock", [SAME, SAME, SAME[:1]]),
        _cell("b1", "mod", [SAME] * 3),
    ]
    assert _run(cells).tolerance == 0.8
    assert _run(cells).cells[0].status == EnumStabilityStatus.UNSTABLE
    loose = _run(cells, data)
    assert loose.tolerance == 0.2
    assert loose.cells[0].status == EnumStabilityStatus.STABLE
    bad = tmp_path / "bad.yaml"
    bad.write_text("other: 1\n")
    with pytest.raises(ValueError, match="jaccard_tolerance"):
        _run(cells, bad)
