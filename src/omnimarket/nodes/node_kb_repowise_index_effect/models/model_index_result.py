# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# [OMN-11914]

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelKBRepoIndexResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool
    commit_sha: str | None = Field(
        default=None, description="HEAD commit SHA of the indexed repo"
    )
    entry_count: int = Field(
        default=0, description="Number of indexed entries reported by the CLI"
    )
    error: str | None = Field(default=None)
