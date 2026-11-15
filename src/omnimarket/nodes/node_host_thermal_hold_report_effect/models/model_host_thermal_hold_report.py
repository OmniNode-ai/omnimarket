# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request, the guard readings and the result of one host thermal hold report run."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class EnumHoldReportError(StrEnum):
    """The typed errors a run carries; each names the part of the guard's state it could not use."""

    LATCH_UNREADABLE = "latch_unreadable"
    LAST_UNREADABLE = "last_unreadable"
    OUTBOX_UNREADABLE = "outbox_unreadable"
    SHAPE_CHANGED = "shape_changed"
    EPISODE_UNREADABLE = "episode_unreadable"
    EPISODE_UNWRITABLE = "episode_unwritable"
    SETTLE_FAILED = "settle_failed"


class EnumGuardReadState(StrEnum):
    """What a read of one guard file found."""

    ABSENT = "absent"
    PRESENT = "present"
    UNREADABLE = "unreadable"
    SHAPE_CHANGED = "shape_changed"


@dataclass(frozen=True, slots=True)
class ModelGuardRead:
    """One guard file read: its state, its parsed object when present, and a short detail when not."""

    state: EnumGuardReadState
    data: dict[str, Any] | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ModelHostThermalHoldReportRequest:
    """One run of the report for ``host``; the ports and the clock are the handler's, the settings are ``overlay``'s."""

    host: str
    overlay: Any
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class ModelHostThermalHoldReportResult:
    """What one run did. ``ok`` is true only when nothing is left undone: no error, no kept row, no refused row and no alarm
    that was decided but not accepted."""

    host: str
    alarm: str  # "trip", "release" or "none": the alarm this run decided
    alarm_status: str  # accepted, duplicate, pending, refused, error, dry-run, or "" when none was decided
    alarm_request_id: str
    alarm_row: str  # the row that was (or in a dry run would be) sent, "" when none
    alarm_detail: str  # what the append command answered besides the status (a refusal text, a receipt state), "" when none
    delivered: int
    kept: int
    deferred: int
    refused: tuple[tuple[str, str], ...]
    errors: tuple[tuple[str, str], ...]
    dry_run: bool
    ok: bool
