# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tick stamps and controller liveness in, the tick-liveness alarm verdict out (OMN-17427)."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ModelTickLivenessRequest(BaseModel):
    """When queue-status last wrote its trend; a None stamp is a tick that left no mark."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    now: AwareDatetime
    trend_at: AwareDatetime | None = None
    # True: the landing controller ticked recently. False: it did not. None: it could not be read.
    controller_live: bool | None = None
    max_age_minutes: int = Field(default=40, gt=0)


class ModelTickLivenessResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    alarm: bool
    state: Literal["LIVE", "STALE", "SKIPPED", "UNKNOWN"]
    stale: tuple[str, ...] = ()
    fix: str = ""
    friction_cells: str = ""
    alert_text: str = ""
