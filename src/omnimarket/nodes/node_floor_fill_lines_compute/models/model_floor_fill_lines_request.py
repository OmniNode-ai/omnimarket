# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request the fill orchestrator builds at S6: what the pass read, did and found."""

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

from omnimarket.nodes.node_floor_fill_lines_compute.models.enum_floor_fill_lines_entry_state import (
    EnumFloorFillLinesEntryState,
)
from omnimarket.nodes.node_floor_fill_lines_compute.models.enum_floor_fill_lines_source import (
    EnumFloorFillLinesSource,
)
from omnimarket.nodes.node_floor_fill_lines_compute.models.enum_floor_fill_lines_start_kind import (
    EnumFloorFillLinesStartKind,
)
from omnimarket.nodes.node_floor_fill_lines_compute.models.enum_floor_fill_lines_verdict import (
    EnumFloorFillLinesVerdict,
)

_FLOOR_KEY = re.compile(r"^floor:([^:\s]+):([^:\s]+)$")
_HOLD = re.compile(r"^(none|open-count|stop-signal:\S.*)$")


def _floor_key(value: str) -> str:
    if _FLOOR_KEY.match(value) is None:
        raise ValueError(f"{value!r} is not a floor key, floor:<line_id>:<task_id>")
    return value


def _hold(value: str) -> str:
    if _HOLD.match(value) is None:
        raise ValueError(
            f"{value!r} is not a hold: none, open-count or stop-signal:<cause>"
        )
    return value


FloorKey = Annotated[str, AfterValidator(_floor_key)]
Hold = Annotated[str, AfterValidator(_hold)]


def line_of_key(key: str) -> str:
    """The ``line_id`` a floor key names."""
    match = _FLOOR_KEY.match(key)
    if match is None:
        raise ValueError(f"{key!r} is not a floor key, floor:<line_id>:<task_id>")
    return match.group(1)


class ModelFloorFillLinesUntickable(BaseModel):
    """A ready task of a line that cannot be ticketed, with the cause T1 gave."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str = Field(min_length=1)
    cause: str = Field(min_length=1)


class ModelFloorFillLinesLine(BaseModel):
    """T1's verdict for one line.

    A ``STARTABLE`` or ``NEEDS-TICKET`` line names its next task; a ``NEEDS-TICKET`` line has
    no ticket yet. An ``UNTICKETABLE`` line lists the tasks that cannot be ticketed. A
    ``NO-STARTABLE`` line carries the cause of why nothing is ready.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    line_id: str = Field(min_length=1, pattern=r"^[^:\s]+$")
    verdict: EnumFloorFillLinesVerdict
    next_task_id: str | None = None
    ticket: str | None = None
    cause: str | None = None
    untickable: tuple[ModelFloorFillLinesUntickable, ...] = ()

    @model_validator(mode="after")
    def _the_verdict_has_its_cells(self) -> Self:
        verdict = self.verdict
        needs_next = verdict in (
            EnumFloorFillLinesVerdict.STARTABLE,
            EnumFloorFillLinesVerdict.NEEDS_TICKET,
        )
        if needs_next and not self.next_task_id:
            raise ValueError(f"a {verdict.value} line names its next task")
        if verdict is EnumFloorFillLinesVerdict.NEEDS_TICKET and self.ticket:
            raise ValueError("a NEEDS-TICKET line has no ticket yet")
        if verdict is EnumFloorFillLinesVerdict.UNTICKETABLE and not self.untickable:
            raise ValueError("an UNTICKETABLE line lists its untickable tasks")
        if verdict is EnumFloorFillLinesVerdict.NO_STARTABLE and not self.cause:
            raise ValueError("a NO-STARTABLE line carries the cause")
        return self


class ModelFloorFillLinesEntry(BaseModel):
    """One key of the fill's state row, as far as the lines need it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: FloorKey
    line_id: str = Field(min_length=1)
    state: EnumFloorFillLinesEntryState
    lane: str | None = None
    ended: bool = False
    reserved_at: AwareDatetime | None = None


class ModelFloorFillLinesStartResult(BaseModel):
    """What this tick did with one key: ``started`` with its lane, or ``failed``/``mint-refused`` with a cause."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: FloorKey
    kind: EnumFloorFillLinesStartKind
    lane: str | None = None
    cause: str | None = None

    @model_validator(mode="after")
    def _the_kind_has_its_cell(self) -> Self:
        if self.kind is EnumFloorFillLinesStartKind.STARTED:
            if not self.lane:
                raise ValueError("a started key names its lane")
        elif not self.cause:
            raise ValueError(f"a {self.kind.value} key names its cause")
        return self


class ModelFloorFillLinesRequest(BaseModel):
    """Everything a pass read and did; the compute only words it, and reads no clock."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    unread: tuple[str, ...] = ()
    lines: tuple[ModelFloorFillLinesLine, ...] = ()
    entries: tuple[ModelFloorFillLinesEntry, ...] = ()
    now: AwareDatetime
    reserved_timeout_seconds: int = Field(gt=0)
    unresolved_keys: tuple[FloorKey, ...] = ()
    start_results: tuple[ModelFloorFillLinesStartResult, ...] = ()
    free_slots_after: int = Field(ge=0)
    hold: Hold

    @model_validator(mode="after")
    def _unread_names_a_source_and_a_cause(self) -> Self:
        sources = {s.value for s in EnumFloorFillLinesSource}
        for item in self.unread:
            source, separator, cause = item.partition(":")
            if source not in sources or not separator or not cause.strip():
                raise ValueError(
                    f"{item!r} is not <source>:<cause> with a source of {sorted(sources)}"
                )
        return self

    @model_validator(mode="after")
    def _unresolved_keys_name_entries(self) -> Self:
        held = {entry.key for entry in self.entries}
        for key in self.unresolved_keys:
            if key not in held:
                raise ValueError(f"unresolved key {key!r} names no entry")
        return self


__all__: list[str] = [
    "FloorKey",
    "Hold",
    "ModelFloorFillLinesEntry",
    "ModelFloorFillLinesLine",
    "ModelFloorFillLinesRequest",
    "ModelFloorFillLinesStartResult",
    "ModelFloorFillLinesUntickable",
    "line_of_key",
]
