# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The correlated tick terminal (OMN-19998)."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict

from omnimarket.models.silent_failure_alarm import (
    ModelDrainerObservation,
    ModelDrainerRepair,
)

from .model_quiet_host_repair import ModelHostCaptureRepair


class ModelSilentFailureAlarmResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    request_id: UUID
    exit_code: int
    # The one stdout line of the tick; empty when the tick refused before observing anything.
    summary: str
    # Lines the tick writes to stderr.
    warnings: tuple[str, ...] = ()
    # The alarms raised this tick (not deduplicated away): placement-share, tick-liveness.
    alarms: tuple[str, ...] = ()
    # The FRICTION rows the tick rendered for those alarms, stamped.
    rows: tuple[str, ...] = ()

    host: str | None = None
    observation: ModelDrainerObservation | None = None
    repair: ModelDrainerRepair | None = None
    capture_repairs: tuple[ModelHostCaptureRepair, ...] = ()
