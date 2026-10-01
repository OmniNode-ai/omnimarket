# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed NoPhantomPathsParams contract."""

from pydantic import BaseModel, ConfigDict, Field


class ModelNoPhantomPathsParams(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path_argument_names: tuple[str, ...] = Field(min_length=1)
    creating_tools: tuple[str, ...]
    line_tolerance: int = Field(ge=0)
