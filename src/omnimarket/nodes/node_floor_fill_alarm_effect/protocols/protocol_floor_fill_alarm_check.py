# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The four reads and writes of the alarm check: the fill's runs, its started lanes, the pool, the ledger."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from omnimarket.nodes.node_floor_fill_alarm_compute.models import (
    ModelFloorFillPoolRead,
    ModelFloorFillRun,
    ModelFloorFillSentLane,
)


class AlarmSourceError(Exception):
    """A source could not be read or written; the check fails by name and writes no row."""


class ProtocolFloorRunSource(Protocol):
    def read_runs(self, now: datetime) -> tuple[ModelFloorFillRun, ...]:
        """The fill's recent runs, any order; raises ``AlarmSourceError`` when they cannot be read."""
        ...


class ProtocolFloorSentLaneSource(Protocol):
    def read_sent_lanes(self) -> tuple[ModelFloorFillSentLane, ...]:
        """The lanes the fill started, with the time of each one's CLAIM when the ledger holds it."""
        ...


class ProtocolFloorPoolReader(Protocol):
    def read_pool(self) -> ModelFloorFillPoolRead:
        """One pool read apart from the fill's runs; a failed read is an unread answer, never a count of 0."""
        ...


class ProtocolAlarmLedger(Protocol):
    def rows(self) -> tuple[str, ...]:
        """The ledger rows, in file order."""
        ...

    def append(self, row: str) -> None:
        """Append one row; raises ``AlarmSourceError`` when the ledger refuses or cannot be written."""
        ...


__all__: list[str] = [
    "AlarmSourceError",
    "ProtocolAlarmLedger",
    "ProtocolFloorPoolReader",
    "ProtocolFloorRunSource",
    "ProtocolFloorSentLaneSource",
]
