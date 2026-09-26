# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared wire contract between the topic sampler and projection."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelTopicActivitySample(BaseModel):
    """One non-empty broker topic observed at one sample time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(min_length=1)
    high_watermark_total: int = Field(ge=0)
    low_watermark_total: int = Field(ge=0)
    messages_last_hour: int = Field(ge=0)
    messages_last_24h: int = Field(ge=0)
    retention_truncated: bool
    newest_message_at: datetime | None = None

    @model_validator(mode="after")
    def _watermarks_are_ordered(self) -> ModelTopicActivitySample:
        if self.high_watermark_total < self.low_watermark_total:
            raise ValueError("high_watermark_total must be >= low_watermark_total")
        return self


class ModelTopicActivitySampleEvent(BaseModel):
    """One bounded part of a complete broker sample."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"
    event_type: Literal["topic-activity-sampled"] = "topic-activity-sampled"
    sample_id: str = Field(min_length=1)
    sampled_at: datetime
    part_index: int = Field(ge=0)
    part_count: int = Field(gt=0)
    sample_interval_seconds: int = Field(gt=0)
    total_topic_count: int = Field(ge=0)
    empty_topic_count: int = Field(ge=0)
    broker_topics: tuple[str, ...] = ()
    topics: tuple[ModelTopicActivitySample, ...] = ()

    @model_validator(mode="after")
    def _part_coordinates_are_valid(self) -> ModelTopicActivitySampleEvent:
        if self.part_index >= self.part_count:
            raise ValueError("part_index must be less than part_count")
        if self.empty_topic_count > self.total_topic_count:
            raise ValueError("empty_topic_count must not exceed total_topic_count")
        return self


__all__ = ["ModelTopicActivitySample", "ModelTopicActivitySampleEvent"]
