# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed ToolCallsWellformedParams contract."""

from pydantic import BaseModel, ConfigDict


class ModelToolCallsWellformedParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    allow_extra_arguments: bool
