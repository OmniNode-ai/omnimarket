# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelDeliveryPosition — terminal offset for one (topic, partition)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelDeliveryPosition(BaseModel):
    """Terminal offset for a single ``(topic, partition)`` after replay.

    Attributes:
        topic: The topic.
        partition: The partition within the topic.
        offset: The maximum offset observed for this topic/partition.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(..., min_length=1, description="Topic.")
    partition: int = Field(..., ge=0, description="Partition within the topic.")
    offset: int = Field(..., ge=0, description="Max offset observed.")


__all__ = ["ModelDeliveryPosition"]
