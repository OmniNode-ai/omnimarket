# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed parity report: empty watcher input cannot prove exact parity."""

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_projection_pr_state.models.model_pr_state_parity_mismatch import (
    ModelPrStateParityMismatch,
)


class ModelPrStateParityReport(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    file_open_prs: int
    projection_open_prs: int
    mismatches: tuple[ModelPrStateParityMismatch, ...]

    @property
    def exact(self) -> bool:
        return self.file_open_prs > 0 and not self.mismatches
