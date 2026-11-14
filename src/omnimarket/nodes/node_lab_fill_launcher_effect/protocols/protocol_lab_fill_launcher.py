# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Port protocols the launcher handler depends on."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from typing import Protocol


class ProtocolLabFillFireFiles(Protocol):
    """Reads the fire's result file, run log and snapshot; writes the latest file."""

    def read_result(self, fire_id: str) -> tuple[bool, bool, str | None]:
        """(exists, nonempty, text) for the fire's own written result file."""
        ...

    def read_log(self, path: str) -> str:
        """The run log text ("" when unreadable)."""
        ...

    def snapshot_present(self, name: str) -> bool:
        """Whether the Linear snapshot file of the day exists."""
        ...

    def latest_path(self) -> str:
        """Where the validated result is published."""
        ...

    def write_latest(self, value: Mapping[str, object]) -> None:
        """Publish the validated result at the latest path."""
        ...


class ProtocolLabFillAlertSender(Protocol):
    """Delivers one alert; returns the delivery's exit code."""

    def send(self, category: str, text: str) -> int: ...


class ProtocolLabFillClock(Protocol):
    """The date the launcher uses for the snapshot name."""

    def today_utc(self) -> date: ...
