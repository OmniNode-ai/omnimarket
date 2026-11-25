# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request, the readings, the declared hold record and the result of one host thermal guard run."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class EnumGuardAction(StrEnum):
    """What one run decided about the hold."""

    HOLD = "hold"
    RELEASE = "release"
    NONE = "none"


class EnumGuardError(StrEnum):
    """The typed errors a run carries. None of them reads as a release: a run that cannot read the host leaves the hold as it is."""

    NO_SENSOR_READING = "no_sensor_reading"
    HOLD_UNREADABLE = "hold_unreadable"
    HOLD_UNWRITABLE = "hold_unwritable"
    EPISODE_UNREADABLE = "episode_unreadable"
    EPISODE_UNWRITABLE = "episode_unwritable"
    ROW_NOT_DELIVERED = "row_not_delivered"
    ROW_REFUSED = "row_refused"
    LANES_NOT_CUT = "lanes_not_cut"


@dataclass(frozen=True, slots=True)
class ModelThermalReading:
    """The host's temperatures: the CPU package (None without the sensor) and each GPU, all in degrees C."""

    at: str
    cpu_c: float | None
    gpu_c: tuple[float, ...]

    @property
    def hottest_c(self) -> float | None:
        values = [v for v in (self.cpu_c, *self.gpu_c) if v is not None]
        return max(values) if values else None


@dataclass(frozen=True, slots=True)
class ModelHoldRecord:
    """The declared hold: what the hold file holds, and what lab placement reads (the `since` stamp is the one it names)."""

    host: str
    since: str
    hold_id: str
    hottest_c: float
    hold_above_c: float
    release_below_c: float

    def as_dict(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "since": self.since,
            "hold_id": self.hold_id,
            "hottest_c": self.hottest_c,
            "hold_above_c": self.hold_above_c,
            "release_below_c": self.release_below_c,
        }


@dataclass(frozen=True, slots=True)
class ModelHostThermalGuardRequest:
    """One run of the guard for ``host``; the ports and the clock are the handler's, the settings are ``overlay``'s."""

    host: str
    overlay: Any
    dry_run: bool = False


@dataclass(frozen=True, slots=True)
class ModelHostThermalGuardResult:
    """What one run did. ``ok`` is true only when nothing is left undone: a reading was taken, the hold file and episode
    file were usable, and every ledger row is delivered (a row that was not delivered is kept for the next run and is never counted)."""

    host: str
    hottest_c: float | None
    action: str
    held: bool
    hold_id: str
    rows: tuple[
        str, ...
    ]  # the rows this run queued (in a dry run, the rows it would queue)
    delivered: int
    pending: int
    refused: tuple[tuple[str, str], ...]
    cut: tuple[str, ...]
    errors: tuple[tuple[str, str], ...]
    dry_run: bool
    ok: bool
