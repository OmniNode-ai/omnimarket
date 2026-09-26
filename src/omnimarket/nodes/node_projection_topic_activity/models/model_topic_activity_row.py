# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One materialized topic-activity row."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_topic_activity.models.enum_topic_activity_state import (
    EnumTopicActivityState,
)


class ModelTopicActivityRow(BaseModel):
    """Current activity for one broker topic; unknown counters remain null."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(min_length=1)
    sampled_at: datetime | None = None
    high_watermark_total: int | None = Field(default=None, ge=0)
    low_watermark_total: int | None = Field(default=None, ge=0)
    retained_messages: int | None = Field(default=None, ge=0)
    messages_since_previous_sample: int | None = None
    rate_per_second: float | None = None
    messages_last_hour: int | None = Field(default=None, ge=0)
    messages_last_24h: int | None = Field(default=None, ge=0)
    rate_last_hour_per_second: float | None = Field(default=None, ge=0)
    retention_truncated: bool | None = None
    newest_message_at: datetime | None = None
    newest_message_age_seconds_at_sample: float | None = Field(default=None, ge=0)
    activity_state: EnumTopicActivityState


__all__ = ["ModelTopicActivityRow"]
