# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Grade the stability of repeated continuations and remove unstable cells in matched pairs.

A cell (boundary, arm) whose three repeats' proposed-call sets have any pairwise Jaccard below
the tolerance read from the data file is UNSTABLE. A cell with fewer repeats than the file's
min_repeats is UNAVAILABLE, which never establishes stability. An UNSTABLE or UNAVAILABLE cell
is removed together with every other arm's cell for the same boundary; a cell with no cell of
another arm at its boundary is an orphan and is removed too, since it cannot be matched. The
exclusion rate per arm counts every removed cell.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

import yaml

from omnimarket.nodes.node_compaction_continuation_replay_effect.handlers.stability import (
    min_pairwise,
)
from omnimarket.nodes.node_compaction_continuation_replay_effect.models import (
    EnumStabilityStatus,
    ModelCellGrade,
    ModelStabilityRequest,
    ModelStabilityResult,
)

__all__ = ["HandlerRepeatStability"]

DEFAULT_DATA_FILE = Path(__file__).resolve().parent.parent / "stability.yaml"


def _read_policy(path: Path) -> tuple[float, int]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "jaccard_tolerance" not in data:
        raise ValueError(f"{path} carries no jaccard_tolerance")
    return float(data["jaccard_tolerance"]), int(data.get("min_repeats", 3))


class HandlerRepeatStability:
    def handle(self, request: ModelStabilityRequest) -> ModelStabilityResult:
        path = (
            Path(request.tolerance_file)
            if request.tolerance_file
            else DEFAULT_DATA_FILE
        )
        tolerance, min_repeats = _read_policy(path)

        status: dict[tuple[str, str], EnumStabilityStatus] = {}
        score: dict[tuple[str, str], float | None] = {}
        arms_at: dict[str, set[str]] = defaultdict(set)
        for cell in request.cells:
            key = (cell.boundary_id, cell.arm)
            arms_at[cell.boundary_id].add(cell.arm)
            lowest = min_pairwise(cell.repeats)
            score[key] = lowest
            if len(cell.repeats) < min_repeats or lowest is None:
                status[key] = EnumStabilityStatus.UNAVAILABLE
            elif lowest < tolerance:
                status[key] = EnumStabilityStatus.UNSTABLE
            else:
                status[key] = EnumStabilityStatus.STABLE

        bad = {b for (b, _), s in status.items() if s != EnumStabilityStatus.STABLE}
        orphans = {b for b, arms in arms_at.items() if len(arms) < 2}
        removed_boundaries = sorted(bad | orphans)

        grades: list[ModelCellGrade] = []
        for cell in request.cells:
            key = (cell.boundary_id, cell.arm)
            reason: str | None = None
            if status[key] != EnumStabilityStatus.STABLE:
                reason = "UNSTABLE_CELL"
            elif cell.boundary_id in bad:
                reason = "MATCHED_REMOVAL"
            elif cell.boundary_id in orphans:
                reason = "ORPHAN_CELL"
            grades.append(
                ModelCellGrade(
                    boundary_id=cell.boundary_id,
                    arm=cell.arm,
                    status=status[key],
                    min_pairwise_jaccard=score[key],
                    retained=reason is None,
                    removal_reason=reason,
                )
            )

        total = len(grades)
        arms = sorted({g.arm for g in grades})
        unstable = [g for g in grades if g.status == EnumStabilityStatus.UNSTABLE]
        rate = len(unstable) / total if total else 0.0
        by_arm = {
            a: sum(1 for g in unstable if g.arm == a) / n
            for a in arms
            if (n := sum(1 for g in grades if g.arm == a))
        }
        excl = {
            a: sum(1 for g in grades if g.arm == a and not g.retained) / n
            for a in arms
            if (n := sum(1 for g in grades if g.arm == a))
        }
        report = (
            f"UNSTABLE rate {rate:.3f} ({len(unstable)}/{total} cells) at tolerance {tolerance}; "
            f"exclusion rate by arm "
            + ", ".join(f"{a}={excl[a]:.3f}" for a in sorted(excl))
        )
        return ModelStabilityResult(
            tolerance=tolerance,
            cells=grades,
            unstable_rate=rate,
            unstable_rate_by_arm=by_arm,
            exclusion_rate_by_arm=excl,
            removed_boundaries=removed_boundaries,
            report=report,
        )
