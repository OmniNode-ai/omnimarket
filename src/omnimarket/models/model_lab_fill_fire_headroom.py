# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One lab-fill fire with the pool hosts' capacity readings, and the records around it (OMN-20867).

node_lab_fill_orchestrator assembles it when a lab-fill fire arrives, from the newest
capacity advertisement each pool host published on the bus; node_lab_fill_plan_compute
decides the fire's headroom plan from it. Neither imports the other's models, so it
lives here.
"""

from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from omnimarket.models.model_host_capacity_advertisement import (
    ModelHostCapacityAdvertisement,
)
from omnimarket.models.model_runtime_tick_schedule import ModelScheduledFire


class ModelLabFillDecidedRecord(BaseModel):
    """Any record on node_lab_fill_plan_compute's decided terminal.

    That terminal carries the schedule's fires and the plans decided from them. A route
    that takes the fires must accept every record there: a plan that failed a strict
    fire model would be classed publisher-malformed and dead-lettered. ``as_fire``
    returns the fire when the record is one.
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    workflow: str | None = None
    fire_id: str | None = None

    def as_fire(self) -> ModelScheduledFire | None:
        try:
            return ModelScheduledFire.model_validate(self.model_dump())
        except ValidationError:
            return None


class ModelLabFillFireHeadroomRequest(BaseModel):
    """A lab-fill fire, the readings held when it arrived and when it was assembled."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fire: ModelScheduledFire
    observed_at: dt.datetime = Field(
        ...,
        description="When the orchestrator assembled the request; reading freshness "
        "is measured against it.",
    )
    readings: tuple[ModelHostCapacityAdvertisement, ...] = ()
    max_lanes: int = Field(default=8, ge=1, le=12)

    @field_validator("observed_at")
    @classmethod
    def _aware(cls, value: dt.datetime) -> dt.datetime:
        if value.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")
        return value


__all__: list[str] = ["ModelLabFillDecidedRecord", "ModelLabFillFireHeadroomRequest"]
