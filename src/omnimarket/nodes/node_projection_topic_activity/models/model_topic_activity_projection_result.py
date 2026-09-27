# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed output of the pure topic-activity fold."""

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_projection_topic_activity.models.model_topic_activity_row import (
    ModelTopicActivityRow,
)


class ModelTopicActivityProjectionResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    row: ModelTopicActivityRow
    applied: bool


__all__ = ["ModelTopicActivityProjectionResult"]
