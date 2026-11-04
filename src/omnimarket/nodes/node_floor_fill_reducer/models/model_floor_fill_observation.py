# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The five observations the fill's orchestrator hands the reducer.

Time and every configured duration or limit come only from the observation: the reducer
reads no clock and no overlay. ``now`` on a reserve is a fresh clock reading taken
immediately before the reserve write, so time spent minting never counts against the
reservation.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from omnimarket.nodes.node_floor_fill_reducer.models.model_floor_fill_state import (
    FloorKey,
)


class _ModelObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    key: FloorKey


class ModelFloorFillTicketStored(_ModelObservation):
    """The task's ticket exists: creates the entry or sets its ticket."""

    kind: Literal["TicketStored"] = "TicketStored"
    ticket: str = Field(min_length=1)


class ModelFloorFillReserveRequested(_ModelObservation):
    """Reserve the key if the line, the slot budget, the brake and the stop signals allow it."""

    kind: Literal["ReserveRequested"] = "ReserveRequested"
    max_fill_lanes_per_line: int = Field(ge=0)
    slot_budget: int = Field(ge=0)
    landing_only: bool
    stop_signal: bool
    sent_window_minutes: int = Field(gt=0)
    now: AwareDatetime


class _ModelAttemptResponse(_ModelObservation):
    """An answer about one offer of a key: it names the attempt and the lane it was sent for."""

    attempt: int = Field(ge=0)
    lane: str = Field(min_length=1)


class ModelFloorFillStartFailed(_ModelAttemptResponse):
    """The process effect answered ``failed``: nothing was started."""

    kind: Literal["StartFailed"] = "StartFailed"


class ModelFloorFillReservedChecked(_ModelAttemptResponse):
    """The ledger was read for the reserved attempt's CLAIM (lane name and ``key=`` cell)."""

    kind: Literal["ReservedChecked"] = "ReservedChecked"
    claim_found: bool
    reserved_timeout_seconds: int = Field(gt=0)
    now: AwareDatetime


class ModelFloorFillSentChecked(_ModelAttemptResponse):
    """The ledger's lane-liveness read for a sent attempt."""

    kind: Literal["SentChecked"] = "SentChecked"
    lane_ended: bool
    sent_window_minutes: int = Field(gt=0)
    now: AwareDatetime


ModelFloorFillObservation = Annotated[
    ModelFloorFillTicketStored
    | ModelFloorFillReserveRequested
    | ModelFloorFillStartFailed
    | ModelFloorFillReservedChecked
    | ModelFloorFillSentChecked,
    Field(discriminator="kind"),
]

__all__: list[str] = [
    "ModelFloorFillObservation",
    "ModelFloorFillReserveRequested",
    "ModelFloorFillReservedChecked",
    "ModelFloorFillSentChecked",
    "ModelFloorFillStartFailed",
    "ModelFloorFillTicketStored",
]
