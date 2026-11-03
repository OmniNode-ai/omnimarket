# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Limits section of the factory floor record: one row per account and engine, each with its source and read time."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

LimitKind = Literal[
    "claude_subscription",
    "cloud_credit",
    "api_credit",
    "codex",
    "glm",
    "lab_model",
    "account_pool",
]
LimitStatus = Literal["READ", "UNKNOWN"]


class ModelFloorLimitValue(BaseModel):
    """One measured value of a row. ``value`` is None when this one value was not read: it draws UNKNOWN, never zero."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    value: float | int | str | bool | None
    unit: str = ""


class ModelFloorLimitRow(BaseModel):
    """One account or engine. ``UNKNOWN`` rows carry no values; ``read_at`` is the last reading ("" when never read)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    row_id: str
    kind: LimitKind
    name: str
    status: LimitStatus
    source: str
    read_at: str
    # Why the row is UNKNOWN (never read, older than one publish cycle, unreadable); empty on a READ row.
    cause: str
    values: tuple[ModelFloorLimitValue, ...]


class ModelFloorIdleHostOut(BaseModel):
    """One host's idle slots now; ``idle`` None when the host was not read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    state: Literal["read", "unread", "out"]
    idle: int | None
    slots: int | None


class ModelFloorLimitsIdle(BaseModel):
    """The idle slots beside the limits: per host now, and the Eastern day's idle slot-hours.

    ``idle_slot_hours`` is accrued from the previous record: its idle count times the time since it, within two publish
    cycles; the time past that is ``unread_minutes`` and is not counted. None when the day has no reading.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["read", "unread"]
    day: str
    hosts: tuple[ModelFloorIdleHostOut, ...]
    idle_total: int | None
    idle_unread: int
    idle_slot_hours: float | None
    unread_minutes: float
    basis: str


class ModelFloorLimits(BaseModel):
    """The Limits section: rows (in a fixed order) and the idle slots."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: str
    publish_cycle_seconds: int
    rows: tuple[ModelFloorLimitRow, ...]
    idle: ModelFloorLimitsIdle
