# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fill's state: one row per ``floor_fill.scope``, ``seq`` and the entries by key."""

from __future__ import annotations

import re
from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    model_validator,
)

from omnimarket.nodes.node_floor_fill_reducer.models.enum_floor_fill_entry_state import (
    EnumFloorFillEntryState,
)

_FLOOR_KEY = re.compile(r"^floor:([^:\s]+):([^:\s]+)$")


def _floor_key(value: str) -> str:
    if _FLOOR_KEY.match(value) is None:
        raise ValueError(f"{value!r} is not a floor key, floor:<line_id>:<task_id>")
    return value


FloorKey = Annotated[str, AfterValidator(_floor_key)]


def line_and_task(key: str) -> tuple[str, str]:
    """The ``(line_id, task_id)`` a floor key names."""
    match = _FLOOR_KEY.match(key)
    if match is None:
        raise ValueError(f"{key!r} is not a floor key, floor:<line_id>:<task_id>")
    return match.group(1), match.group(2)


class ModelFloorFillEntry(BaseModel):
    """One key of the fill. ``attempt`` counts offers of the key and only rises."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: FloorKey
    state: EnumFloorFillEntryState = EnumFloorFillEntryState.NONE
    lane: str | None = None
    attempt: int = Field(default=0, ge=0)
    ticket: str | None = None
    ended: bool = False
    reserved_at: AwareDatetime | None = None
    sent_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _dispatch_state_is_whole(self) -> Self:
        dispatched = self.state is not EnumFloorFillEntryState.NONE
        if dispatched != (self.lane is not None):
            raise ValueError(
                "an entry has a lane exactly while it has a dispatch state"
            )
        if dispatched and self.attempt < 1:
            raise ValueError("a dispatched entry is at attempt one or later")
        if (self.state is EnumFloorFillEntryState.RESERVED) != (
            self.reserved_at is not None
        ):
            raise ValueError("reserved_at is set exactly while the entry is RESERVED")
        if (self.state is EnumFloorFillEntryState.SENT) != (self.sent_at is not None):
            raise ValueError("sent_at is set exactly while the entry is SENT")
        if self.ended and self.state is not EnumFloorFillEntryState.SENT:
            raise ValueError("only a SENT entry can have ended")
        return self


class ModelFloorFillState(BaseModel):
    """The row. ``seq`` rises by one per accepted observation; one writer (T13) holds it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(default=0, ge=0)
    entries: dict[str, ModelFloorFillEntry] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one_entry_per_key(self) -> Self:
        for key, entry in self.entries.items():
            if key != entry.key:
                raise ValueError(f"entry {entry.key!r} is filed under {key!r}")
        return self


__all__: list[str] = [
    "FloorKey",
    "ModelFloorFillEntry",
    "ModelFloorFillState",
    "line_and_task",
]
