# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A repository and branch that must require the review verdict context."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelBranchProtectionReviewVerdictRequirement(BaseModel):
    """One (repo, branch) pair that must carry the review verdict context."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str
    branch: str


__all__ = ["ModelBranchProtectionReviewVerdictRequirement"]
