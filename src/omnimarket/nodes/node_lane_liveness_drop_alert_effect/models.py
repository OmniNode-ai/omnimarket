# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models for node_lane_liveness_drop_alert_effect."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelLaneDropSlackCommand(BaseModel):
    """One ``onex.cmd.omnimarket.slack-publish.v1`` command for a lane drop."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    channel: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    idempotency_key: str = Field(..., min_length=1)
    correlation_id: UUID


__all__ = ["ModelLaneDropSlackCommand"]
