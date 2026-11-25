# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The report step: one host's pruner output and disk reading become one row, with a disk alarm past the bar.

The pruner's output is the completed event the worktree reconcile node prints (scanned, removed, kept, needs_human,
failures, freed_bytes). A host whose pruner failed after every attempt gets a failed row carrying the last error and no
alarm: there is no reading to compare. The disk reading is taken here, after the pruner ran.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable

from omnimarket.nodes.node_worktree_prune_temporal_effect.models.model_worktree_prune_temporal import (
    ModelDiskAlarm,
    ModelPruneHostRow,
    ModelPruneReportRequest,
)

DiskUsedPercent = Callable[[str], float]


def disk_used_percent(path: str) -> float:
    usage = shutil.disk_usage(path)
    return round(100.0 * usage.used / usage.total, 2)


def _completed(output: str) -> dict[str, object]:
    """The pruner's result object: the line itself, or its ``completed_event`` member."""
    document = json.loads(output)
    if not isinstance(document, dict):
        raise ValueError("the pruner printed no JSON object")
    inner = document.get("completed_event")
    return inner if isinstance(inner, dict) else document


def _count(completed: dict[str, object], key: str) -> int:
    value = completed.get(key, 0)
    return int(value) if isinstance(value, int | float) else 0


class HandlerPruneReport:
    def __init__(self, disk_reader: DiskUsedPercent = disk_used_percent) -> None:
        self._disk = disk_reader

    def handle(self, request: ModelPruneReportRequest) -> ModelPruneHostRow:
        if request.error is not None or request.pruner_output is None:
            return ModelPruneHostRow(
                step_key=request.step_key,
                host=request.host,
                status="failed",
                error=request.error or "the pruner printed nothing",
            )
        try:
            completed = _completed(request.pruner_output)
        except ValueError as exc:  # json.JSONDecodeError is a ValueError
            return ModelPruneHostRow(
                step_key=request.step_key,
                host=request.host,
                status="failed",
                error=f"unreadable pruner output: {exc}",
            )
        used = self._disk(request.disk_path)
        alarm = (
            ModelDiskAlarm(
                host=request.host,
                used_percent=used,
                bar_percent=request.disk_bar_percent,
            )
            if used > request.disk_bar_percent
            else None
        )
        return ModelPruneHostRow(
            step_key=request.step_key,
            host=request.host,
            status="ok",
            scanned=_count(completed, "scanned"),
            removed=_count(completed, "removed")
            + _count(completed, "pinned_and_removed"),
            kept=_count(completed, "kept"),
            needs_human=_count(completed, "needs_human"),
            failures=_count(completed, "failures"),
            freed_bytes=_count(completed, "freed_bytes"),
            disk_used_percent=used,
            alarm=alarm,
        )


__all__ = ["HandlerPruneReport", "disk_used_percent"]
