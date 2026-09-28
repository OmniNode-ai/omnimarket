# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fold's output: one row per record of the event (OMN-19961)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_projection_lab_container_memory.models.model_container_memory_row import (
    ModelContainerMemoryRow,
)


class ModelContainerMemoryFoldResult(BaseModel):
    """The rows one memory event derives, ordered by ``record_key``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[ModelContainerMemoryRow, ...]


__all__ = ["ModelContainerMemoryFoldResult"]
