# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local git reference request for the check-only prerequisite."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelTicketContractPrerequisiteRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    head: str = Field(default="HEAD", min_length=1)
    base: str = Field(default="origin/dev", min_length=1)
    title: str | None = None
