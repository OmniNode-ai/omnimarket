# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the owner count: the overlay, one ledger row, the request and the result."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelFloorFillOwnerCountOverlay(BaseModel):
    """The node overlay: the window and the row kinds that show a lane is alive, supplied by the deployment.

    Read from ``<root>/node_floor_fill_owner_count_compute/overlay.yaml``; the package ships no value for either
    setting, and ``emission_piece`` names the floor piece the node's chain is counted under.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lane_write_window_minutes: int = Field(gt=0)
    liveness_kinds: tuple[str, ...] = Field(min_length=1)
    emission_piece: int = Field(ge=1, le=18)

    @field_validator("liveness_kinds")
    @classmethod
    def _kinds_are_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(
            not kind.strip() or kind != kind.strip() or kind != kind.upper()
            for kind in value
        ):
            raise ValueError("a liveness kind is a non-empty upper-case row kind")
        return value


class ModelFloorFillLedgerRow(BaseModel):
    """One ledger row reduced to what liveness reads: when, which kind, which lane wrote it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    at: datetime
    kind: str = Field(min_length=1)
    lane: str = Field(min_length=1)


class ModelFloorFillLaneWrites(BaseModel):
    """What one read of the ledger window returned: the rows with a lane, and the lines it could not use."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[ModelFloorFillLedgerRow, ...] = ()
    lines_read: int = Field(default=0, ge=0)
    lines_skipped: int = Field(default=0, ge=0)


class ModelFloorFillOwnerCountRequest(BaseModel):
    """``as_of`` is the instant the window ends at; unset means the injected clock's reading (a replay sets it)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: datetime | None = None

    @field_validator("as_of")
    @classmethod
    def _as_of_is_timezone_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("as_of must carry a timezone")
        return value


class ModelFloorFillOwnerCountResult(BaseModel):
    """The lanes that wrote inside the window, and the one count the fill reports as owned and as running."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: datetime
    window_minutes: int = Field(gt=0)
    lanes: tuple[str, ...]
    owned_count: int = Field(ge=0)
    running_count: int = Field(ge=0)
    lines_read: int = Field(ge=0)
    lines_skipped: int = Field(ge=0)
