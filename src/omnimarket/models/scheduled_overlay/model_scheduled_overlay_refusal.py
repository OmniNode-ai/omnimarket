# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The typed refusal a scheduled effect latches when an overlay it needs is absent (OMN-23587)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelScheduledOverlayRefusal(BaseModel):
    """One missing-overlay refusal: which node, which error type, and the message that names the setting."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_name: str = Field(description="The node whose overlay was not found.")
    error_type: str = Field(description="The exception type that refused the load.")
    detail: str = Field(
        description="The exception message: it names the setting and how to supply it, never values."
    )


__all__ = ["ModelScheduledOverlayRefusal"]
