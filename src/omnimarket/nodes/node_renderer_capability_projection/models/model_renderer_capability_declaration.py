# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Renderer capability declaration command payload (OMN-13131 / W5).

A renderer thin-publishes one of these onto the renderer-capability-declared
command topic (constant ``RENDERER_CAPABILITY_DECLARED_TOPIC_V1`` in
``omnimarket.events.topics``) as a periodic heartbeat. The sole-writer reducer
``node_renderer_capability_projection`` folds each declaration into the
heartbeat-backed Renderer Capability Registry projection.

The declaration composes the shipped core primitive
``ModelRendererCapabilityContract`` (the *what a renderer can render* surface)
with a monotonic ``declared_at`` heartbeat timestamp. The reducer never invents
capability shape; it folds the contract the renderer advertised.
"""

from __future__ import annotations

from datetime import datetime

from omnibase_core.models.dashboard.model_renderer_capability_contract import (
    ModelRendererCapabilityContract,
)
from pydantic import BaseModel, ConfigDict, Field

__all__ = ["ModelRendererCapabilityDeclaration"]


class ModelRendererCapabilityDeclaration(BaseModel):
    """One renderer capability heartbeat declaration (the cmd-topic payload).

    ``capability`` is the renderer's advertised capability surface (the core
    primitive); ``declared_at`` is the heartbeat instant the renderer emitted
    this declaration. The reducer keys the projection on
    ``capability.renderer_id`` and uses ``declared_at`` as the freshness anchor.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    capability: ModelRendererCapabilityContract = Field(
        ...,
        description="The renderer's advertised capability surface (core primitive)",
    )
    declared_at: datetime = Field(
        ...,
        description="Heartbeat instant this declaration was emitted by the renderer",
    )
