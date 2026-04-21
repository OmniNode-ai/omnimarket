# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelPrSnapshotResult — aggregate result from the PR snapshot effect node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_merge_sweep.handlers.handler_merge_sweep import (
    ModelPRInfo,
)
from omnimarket.nodes.node_pr_snapshot_effect.models.model_pr_stall_event import (
    ModelPrStallEvent,
)
from omnimarket.nodes.node_pr_snapshot_effect.models.model_repo_scan_result import (
    ModelRepoScanResult,
)


class ModelPrSnapshotResult(BaseModel):
    """Aggregate result from scanning all repos for PRs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo_results: tuple[ModelRepoScanResult, ...] = Field(
        default_factory=tuple, description="Per-repo scan results."
    )
    stall_events: tuple[ModelPrStallEvent, ...] = Field(
        default_factory=tuple,
        description="PRs detected as stalled (shape-identical across consecutive snapshots).",
    )

    @property
    def all_prs(self) -> list[ModelPRInfo]:
        """All PRs across all repos, for direct wiring into ModelMergeSweepRequest."""
        return [pr for result in self.repo_results for pr in result.prs]

    @property
    def failed_repos(self) -> list[str]:
        """Repos that failed to scan."""
        return [r.repo for r in self.repo_results if not r.success]

    @property
    def total_prs(self) -> int:
        """Total number of PRs found across all repos."""
        return sum(len(r.prs) for r in self.repo_results)


__all__: list[str] = ["ModelPrSnapshotResult"]
