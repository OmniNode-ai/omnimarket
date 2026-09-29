# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One row of ``omninode_internal.lab_container_memory_window`` (OMN-19961)."""

from __future__ import annotations

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.nodes.node_projection_lab_container_memory.models.model_lane_container_memory_event import (
    ModelLaneContainerCiRunWire,
)


class ModelContainerMemoryRow(BaseModel):
    """One container's memory counters for one census window.

    One row per record per window, never one per container: the question the
    table serves is which windows carried CI jobs and what each lane container
    peaked at across them, and a latest-only row discards every earlier window.
    ``projected_at`` is not here: it is the writer's clock, and the fold has
    none.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    record_key: str = Field(min_length=1)
    host: str = Field(min_length=1)
    host_boot_id: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    container_id: str = Field(min_length=1)
    container_name: str = Field(min_length=1)
    container_started_at: AwareDatetime
    limit_bytes: int | None
    peak_bytes: int
    peak_window_start: AwareDatetime
    peak_window_end: AwareDatetime
    window_start: AwareDatetime
    window_end: AwareDatetime
    max_total: int
    max_delta: int
    oom_kill_total: int
    oom_kill_delta: int
    #: Every CI job on this host inside ``[window_start, window_end]``. The
    #: same list on every row of one window, so a single row answers which
    #: jobs ran beside that container's peak.
    ci_runs: tuple[ModelLaneContainerCiRunWire, ...]


__all__ = ["ModelContainerMemoryRow"]
