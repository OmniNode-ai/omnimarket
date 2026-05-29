# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Typed command payload for projection query requests."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelProjectionQueryRequest(BaseModel):
    """Read-only projection query request."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    shape: str = Field(..., min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


__all__ = ["ModelProjectionQueryRequest"]
