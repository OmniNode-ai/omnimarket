# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The worktree whose pending push range the pre-push hooks are run against."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelPrepushPreflightRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    worktree: str
    # The ref the range starts from when the branch has no upstream; none means the clone's origin/HEAD, the landing branch.
    base_ref: str | None = None
    timeout_s: float = Field(default=300.0, gt=0)
    finding_lines: int = Field(default=20, gt=0)
