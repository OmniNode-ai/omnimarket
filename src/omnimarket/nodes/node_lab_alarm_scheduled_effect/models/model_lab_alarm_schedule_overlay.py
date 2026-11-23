# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay the lab alarm scheduled effect reads: which alarm overlay to run."""

from __future__ import annotations

from pydantic import ConfigDict, Field, model_validator

from omnimarket.models.schedule_trigger.model_schedule_trigger import (
    ModelScheduleDeclaration,
)


class ModelLabAlarmScheduleOverlay(ModelScheduleDeclaration):
    """The broker-lag schedule, alarm overlay id and optional restart probe schedule."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    alarm_overlay_id: str = Field(min_length=1)
    container_restarts_probe: ModelScheduleDeclaration | None = None

    @model_validator(mode="after")
    def _distinct_jobs(self) -> ModelLabAlarmScheduleOverlay:
        probe = self.container_restarts_probe
        if probe is not None and probe.job_id == self.job_id:
            raise ValueError("the restart probe and broker lag need distinct job ids")
        return self


__all__: list[str] = [
    "ModelLabAlarmScheduleOverlay",
]
