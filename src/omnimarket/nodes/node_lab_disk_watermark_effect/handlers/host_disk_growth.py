# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Growth rate of a host's disk against its admission floor (OMN-21663).

A second reading of the same host, later and with less free space, says how fast the disk is filling and how long
until it reaches the floor. A span shorter than the declared minimum, or a span that is not positive, makes no rate.
"""

from __future__ import annotations

from datetime import datetime

from omnimarket.nodes.node_lab_disk_watermark_effect.models.model_lab_disk_watermark import (
    ModelVolumeGrowth,
)


def volume_growth(
    previous_read_at: datetime,
    previous_free_bytes: int,
    now: datetime,
    free_bytes: int,
    floor_bytes: float,
    min_span_minutes: int,
) -> ModelVolumeGrowth | None:
    """The growth of one volume between two readings, or None when the span is too short or not positive."""
    span_minutes = (now - previous_read_at).total_seconds() / 60
    if span_minutes <= 0 or span_minutes < min_span_minutes:
        return None
    span_hours = span_minutes / 60
    free_gb_per_hour = (previous_free_bytes - free_bytes) / 1024**3 / span_hours
    if free_gb_per_hour <= 0:
        hours_to_floor: float | None = None
    else:
        hours_to_floor = max(
            0.0, (free_bytes - floor_bytes) / 1024**3 / free_gb_per_hour
        )
    return ModelVolumeGrowth(
        path="",
        free_gb_per_hour=free_gb_per_hour,
        hours_to_floor=hours_to_floor,
        span_minutes=span_minutes,
    )
