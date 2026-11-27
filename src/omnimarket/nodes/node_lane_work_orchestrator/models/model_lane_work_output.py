# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One output a receipt lists: published once, matched by its output id (OMN-26721)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelLaneWorkOutput(BaseModel):
    """``role`` names the topic (read from the contract); ``payload`` is the wire dict."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    output_id: str = Field(..., min_length=1, max_length=160)
    role: str = Field(..., min_length=1)
    payload: dict[str, Any]


__all__: list[str] = ["ModelLaneWorkOutput"]
