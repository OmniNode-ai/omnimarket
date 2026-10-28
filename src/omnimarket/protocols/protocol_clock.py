# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The clock a node reads the time through, so a test or a lab binding can swap it."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class ProtocolClock(Protocol):
    def now(self) -> datetime: ...


__all__: list[str] = ["ProtocolClock"]
