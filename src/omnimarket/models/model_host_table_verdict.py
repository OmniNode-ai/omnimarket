# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared verdict of the lab host table for one host (OMN-17232)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelHostTableVerdict(BaseModel):
    """Whether the host table lets one host take the unit, and the host's own memory bar."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host_name: str
    eligible: bool
    reason: str
    #: The row's own memory admission bar in GB, replacing the caller's bar; None: the caller's.
    min_free_gb: float | None = None
    #: The row's own CPU hold bar in degrees C (``max_cpu_c``): new heavy work is held on a host above it; None: no bar.
    max_cpu_c: float | None = None
    speed: float = 1.0
