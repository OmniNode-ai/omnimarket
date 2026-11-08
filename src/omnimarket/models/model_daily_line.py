# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the daily line (rebuild plan section 11, OMN-22925).

The daily line is one sentence a node computes at 05:30 Eastern from three sources: piece 1's reading on the floor
record, the state of join J1 (piece 1 to piece 2) and the count of claims with no TERMINAL. A source that was not read is
``None`` here and renders ``UNKNOWN``; no part of the line is ever an empty cell.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

UNKNOWN = "UNKNOWN"

DailyLinePartName = Literal["piece1", "j1", "claims"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelDailyLinePiece(_Frozen):
    """Piece 1 as the floor record reads it: its reading state and the floor's own words for why."""

    state: str
    detail: str = ""


class ModelDailyLineRequest(_Frozen):
    """What the daily line is computed from. ``None`` is a source that was not read."""

    piece1: ModelDailyLinePiece | None = None
    j1_state: str | None = None
    open_claims: int | None = Field(default=None, ge=0)


class ModelDailyLinePart(_Frozen):
    """One part of the line: its rendered text and whether its source was read."""

    name: DailyLinePartName
    text: str = Field(min_length=1)
    read: bool


class ModelDailyLineResult(_Frozen):
    """The finished sentence and its three parts."""

    line: str = Field(min_length=1)
    parts: tuple[ModelDailyLinePart, ...]
    unknown_parts: tuple[DailyLinePartName, ...] = ()


class ModelDailyLineRunCommand(_Frozen):
    """One run of the daily line. It carries no value: the sources are read from the registry home."""


class ModelDailyLinePublication(_Frozen):
    """What one scheduled run published: the line, where, when and the ledger row it wrote."""

    schema_id: Literal["omni.daily-line/1"] = "omni.daily-line/1"
    generated_at: datetime
    line: str = Field(min_length=1)
    parts: tuple[ModelDailyLinePart, ...]
    unknown_parts: tuple[DailyLinePartName, ...] = ()
