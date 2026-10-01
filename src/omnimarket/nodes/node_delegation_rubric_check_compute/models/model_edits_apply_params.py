# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed EditsApplyParams contract."""

from pydantic import BaseModel, ConfigDict, Field


class ModelEditsApplyParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    edit_tools: tuple[str, ...] = Field(min_length=1)
