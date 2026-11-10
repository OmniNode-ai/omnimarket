# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor document as an event on the bus, and the schedule's overlay (OMN-23072)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.floor_17a.model_floor_17a import ModelFloorSimple


class ModelFloorSimpleDocumentEvent(BaseModel):
    """One computed floor document: what the scheduled compute emits and the projection holds."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    computed_at: datetime = Field(
        description="The tick time the document was computed for."
    )
    document: ModelFloorSimple
    tick_id: str = Field(description="The runtime tick that scheduled the computation.")


class ModelFloorDocumentApplied(BaseModel):
    """What the projection reports after one document event: held, or refused as older than the held one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    applied: bool
    computed_at: datetime
    held_computed_at: datetime | None
    reason: str


class ModelFloorScheduleOverlay(BaseModel):
    """The floor schedule's overlay: the cadence of the scheduled compute and where its readings come from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_interval_seconds: int = Field(gt=0)
    reading_source: str = Field(
        pattern=r"^[a-z][a-z0-9_]*$",
        description="The reading source profile the schedule binds: unread (in-memory default) or a mirror profile.",
    )
    floor_overlay_id: str = Field(
        description="The overlay id of the floor overlay the compute reads (ModelFloorSimpleOverlay).",
    )


__all__ = [
    "ModelFloorDocumentApplied",
    "ModelFloorScheduleOverlay",
    "ModelFloorSimpleDocumentEvent",
]
