# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire models emitted by the topic-activity broker sampler (OMN-19716)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelTopicActivitySampleTrigger(BaseModel):
    """The runtime heartbeat used only as a clock carrier."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    service_name: str | None = None
    node_id: str | None = None


class ModelTopicActivitySamplingPolicy(BaseModel):
    """Contract-owned cadence and maximum serialized event size."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sample_interval_seconds: int = Field(gt=0)
    max_event_bytes: int = Field(gt=1024, lt=1_000_000)


__all__ = [
    "ModelTopicActivitySampleTrigger",
    "ModelTopicActivitySamplingPolicy",
]
