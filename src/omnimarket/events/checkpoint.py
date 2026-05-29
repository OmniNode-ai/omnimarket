# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared checkpoint command models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelCheckpointRequest(BaseModel):
    """Request to save, load, or list checkpoints."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checkpoint_id: str = Field(description="Unique identifier for the checkpoint")
    action: str = Field(description="Operation to perform: save | load | list")
    payload: dict[str, Any] | None = Field(
        default=None,
        description="Checkpoint payload for save operations; null for load/list",
    )


__all__ = ["ModelCheckpointRequest"]
