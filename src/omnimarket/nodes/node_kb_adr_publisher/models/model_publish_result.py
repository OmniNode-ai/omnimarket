# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

# [OMN-11808]

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelKBADRPublishResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool
    adr_count: int = Field(default=0)
    pr_url: str | None = Field(default=None)
    branch: str | None = Field(default=None)
    error: str | None = Field(default=None)
