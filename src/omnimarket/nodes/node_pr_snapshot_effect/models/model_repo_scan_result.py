# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelRepoScanResult — result of scanning a single repo."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_merge_sweep.handlers.handler_merge_sweep import (
    ModelPRInfo,
)


class ModelRepoScanResult(BaseModel):
    """Result of scanning a single GitHub repo for PRs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., description="GitHub repo (org/repo format).")
    prs: tuple[ModelPRInfo, ...] = Field(
        default_factory=tuple, description="PRs found in this repo."
    )
    error: str | None = Field(
        default=None, description="Error message if scan failed for this repo."
    )

    @property
    def success(self) -> bool:
        """Whether the scan succeeded (no error)."""
        return self.error is None


__all__: list[str] = ["ModelRepoScanResult"]
