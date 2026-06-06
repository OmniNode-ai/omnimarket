# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Output model for node_checkpoint_compute."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelCheckpointResult(BaseModel):
    """Result of a checkpoint save, load, or list operation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    checkpoint_id: str = Field(description="Checkpoint identifier (echoed or resolved)")
    action: str = Field(description="Action that was executed")
    data: dict[str, Any] | None = Field(
        default=None,
        description="Deserialized checkpoint data for load; null for save/list",
    )
    checkpoint_list: list[str] = Field(
        default_factory=list,
        description="List of checkpoint IDs for list action; empty otherwise",
    )
