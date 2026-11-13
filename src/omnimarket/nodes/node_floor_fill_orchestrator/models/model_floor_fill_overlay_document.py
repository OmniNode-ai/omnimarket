# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The whole overlay file, both sections, so the overlays repository loads the file through the node's own model."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_config import (
    ModelFloorFillConfig,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_live_settings import (
    ModelFloorFillLiveSettings,
)


class ModelFloorFillOverlayDocument(BaseModel):
    """The overlay file's two sections (``floor_fill`` and ``floor_fill_live``) as one document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    floor_fill: ModelFloorFillConfig
    floor_fill_live: ModelFloorFillLiveSettings


__all__: list[str] = [
    "ModelFloorFillOverlayDocument",
]
