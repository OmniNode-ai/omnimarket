# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The wall clock of the live binding: ``ProtocolFloorClock`` answered by the system time."""

from __future__ import annotations

from datetime import UTC, datetime


class SystemFloorClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


__all__: list[str] = ["SystemFloorClock"]
