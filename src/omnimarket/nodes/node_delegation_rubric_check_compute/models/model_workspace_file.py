# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed WorkspaceFile contract."""

from pydantic import BaseModel, ConfigDict, Field


class ModelWorkspaceFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    line_count: int = Field(ge=0)
