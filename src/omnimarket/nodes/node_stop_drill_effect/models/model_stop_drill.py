# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the hourly stop drill (OMN-22943): its overlay, its request and its result."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.lab_test_harness import (
    ModelLabTestHarnessEmission,
    ModelLabTestHarnessScheduleHost,
)
from omnimarket.models.model_process_spine_reading import (
    ModelDrillObservation,
    ModelProcessSpineReading,
)

_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


class ModelStopDrillOverlay(BaseModel):
    """Every setting of the drill. The node ships none of these values."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay_revision: str = Field(min_length=1)
    #: The piece the test instance stops, and the piece a lane must still be admitted on.
    drill_piece: str
    control_piece: str
    store_limits: dict[str, int]
    reservation_ttl_seconds: int = Field(ge=1)
    #: A reading older than this is NOT WORKING.
    max_age_seconds: int = Field(gt=0)
    #: True once the replay over the recorded dispatch history writes a result the reading can read.
    replay_required: bool
    #: The floor's journal, the chain identity and the ledger rows of the same events. The ledger is required.
    emission: ModelLabTestHarnessEmission
    schedule_hosts: tuple[ModelLabTestHarnessScheduleHost, ...] = ()
    unit_environment: dict[str, str] = Field(default_factory=dict)

    @field_validator("drill_piece", "control_piece")
    @classmethod
    def _plain(cls, value: str) -> str:
        if not _NAME.match(value):
            raise ValueError(f"not a plain name: {value!r}")
        return value


class ModelStopDrillRequest(BaseModel):
    """One run request: an id, the time it was asked and what asked."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1)
    requested_at: datetime
    trigger: str = Field(min_length=1)


class ModelStopDrillResult(BaseModel):
    """What one run saw, how it read, and what the ledger took."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    reading: ModelProcessSpineReading
    observation: ModelDrillObservation
    #: The same drill against an instance that accepts the drill's lane: it must read NOT WORKING.
    planted_reading: ModelProcessSpineReading
    planted_red: bool
    #: The drill's own verdict: the run's reading, or NOT WORKING when the planted fault did not read red.
    verdict: str
    reason: str
    ledger_rows: tuple[str, ...] = ()
