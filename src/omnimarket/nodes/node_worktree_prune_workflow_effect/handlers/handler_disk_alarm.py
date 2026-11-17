# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The disk alarm of a prune run: a host whose use after the run is past its bar raises one alarm."""

from __future__ import annotations

from omnimarket.nodes.node_worktree_prune_workflow_effect.models.model_worktree_prune_workflow import (
    ModelDiskAlarm,
    ModelPruneHostTarget,
)


def disk_alarm(
    target: ModelPruneHostTarget, used_percent: float
) -> ModelDiskAlarm | None:
    """An alarm when the use is past the bar, none at or below it."""
    if used_percent > target.disk_bar_percent:
        return ModelDiskAlarm(
            host=target.host,
            used_percent=used_percent,
            bar_percent=target.disk_bar_percent,
        )
    return None


__all__ = ["disk_alarm"]
