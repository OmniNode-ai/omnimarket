# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What one writer topic apply planned, created and found already present."""

from pydantic import BaseModel, ConfigDict


class ModelWriterTopicApplyResult(BaseModel):
    """The planned writer topics, split into those created and those already on the broker."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    planned: tuple[str, ...]
    created: tuple[str, ...]
    existing: tuple[str, ...]
    num_partitions: int
    replication_factor: int
