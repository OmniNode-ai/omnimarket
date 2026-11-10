# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Port of node_broker_auth_refusal_reading_effect: the broker's log lines that match the needle."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


class BrokerLogError(RuntimeError):
    """The broker's log could not be read."""


class ProtocolBrokerLog(Protocol):
    def matching_lines(self, needle: str, window_seconds: int) -> tuple[str, ...]:
        """Log lines of the window that hold ``needle``, each starting with its RFC 3339 UTC timestamp.

        Raises ``BrokerLogError`` with a typed reason when the log cannot be read.
        """
        ...


def parse_stamp(text: str) -> datetime | None:
    """An RFC 3339 UTC stamp as docker writes it (``2026-11-09T15:32:43.997373838Z``), or None when unreadable."""
    head = text.strip()
    if len(head) < 19 or head[4] != "-" or head[10] != "T":
        return None
    body = head[:19]
    fraction = head[19:].rstrip("Z")
    if fraction.startswith(".") and fraction[1:].isdigit():
        body += "." + fraction[1:7].ljust(6, "0")
    try:
        return datetime.fromisoformat(body).replace(tzinfo=UTC)
    except ValueError:
        return None


@dataclass(frozen=True)
class ControlRun:
    """One run of the deploy's unauthenticated negative control (OMN-18012): its container and its UTC span."""

    container: str
    started_at: datetime
    finished_at: datetime


class ProtocolControlRuns(Protocol):
    def runs(self) -> tuple[ControlRun, ...]:
        """The control runs the host still holds, by the control's container.

        An absent container is no runs (no deploy has run it); raises ``BrokerLogError`` with a typed reason
        when the host cannot be asked.
        """
        ...


__all__ = [
    "BrokerLogError",
    "ControlRun",
    "ProtocolBrokerLog",
    "ProtocolControlRuns",
    "parse_stamp",
]
