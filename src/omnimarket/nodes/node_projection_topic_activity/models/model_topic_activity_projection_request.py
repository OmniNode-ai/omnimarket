# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input to the pure topic-activity fold."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.topic_activity import ModelTopicActivitySample
from omnimarket.nodes.node_projection_topic_activity.models.model_topic_activity_row import (
    ModelTopicActivityRow,
)


class ModelTopicActivityProjectionRequest(BaseModel):
    """Prior row plus the next observation, all explicit fold inputs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(min_length=1)
    sampled_at: datetime | None = None
    previous_row: ModelTopicActivityRow | None = None
    sample: ModelTopicActivitySample | None = None


__all__ = ["ModelTopicActivityProjectionRequest"]
