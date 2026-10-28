# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The clock a node reads the time through, so a test or a lab binding can swap it."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable


@runtime_checkable
class ProtocolClock(Protocol):
    """One read of the current time."""

    def now(self) -> datetime: ...


__all__: list[str] = ["ProtocolClock"]
