# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Port of node_broker_auth_refusal_reading_effect: the broker's log lines that match the needle."""

from __future__ import annotations

from typing import Protocol


class BrokerLogError(RuntimeError):
    """The broker's log could not be read."""


class ProtocolBrokerLog(Protocol):
    def matching_lines(self, needle: str, window_seconds: int) -> tuple[str, ...]:
        """Log lines of the window that hold ``needle``, each starting with its RFC 3339 UTC timestamp.

        Raises ``BrokerLogError`` with a typed reason when the log cannot be read.
        """
        ...


__all__ = ["BrokerLogError", "ProtocolBrokerLog"]
