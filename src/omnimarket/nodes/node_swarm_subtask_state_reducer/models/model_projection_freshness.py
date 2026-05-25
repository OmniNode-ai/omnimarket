# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Projection freshness model — emitted with swarm-subtask-projection-applied.v1."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumFreshnessState(StrEnum):
    FRESH = "fresh"
    STALE = "stale"
    DEGRADED = "degraded"


class ModelProjectionFreshness(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    projection_cursor: str
    source_event_id: str
    source_topic: str
    source_partition: int
    source_offset: int
    freshness_state: EnumFreshnessState
    reducer_version: str
    observed_at: str


__all__: list[str] = ["EnumFreshnessState", "ModelProjectionFreshness"]
