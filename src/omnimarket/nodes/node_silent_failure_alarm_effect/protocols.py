# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The same per-host observation and installer boundary for local and remote drainers."""

from typing import Protocol

from omnimarket.models.silent_failure_alarm import (
    ModelDrainerObservation,
    ModelDrainerRepair,
)


class ProtocolDrainerWatch(Protocol):
    def observe(self) -> ModelDrainerObservation:
        """Return what this host's drainer looks like now."""

    def repair(self) -> ModelDrainerRepair:
        """Repair this host's drainer and return what was done."""


class ProtocolAlarmLedger(Protocol):
    """The work ledger as the alarm uses it: a window of rows read, one row appended, both over the bus."""

    def read_rows(self, window_hours: int) -> str:
        """Return the ledger rows of the last ``window_hours`` hours, one row per line."""

    def append(self, row: str) -> tuple[bool, str]:
        """Append one row; return whether it was accepted and the reason it was or was not."""
