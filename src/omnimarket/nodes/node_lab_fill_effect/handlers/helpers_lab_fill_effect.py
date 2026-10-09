# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared helpers of the lab-fill effect handlers (OMN-20668)."""

from __future__ import annotations

import time
from datetime import UTC, datetime

from ..protocols import ProtocolLabFillClock


class SystemClock:
    """The wall clock; tests pass a fixed one."""

    def now_epoch_s(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


def iso_utc(epoch_s: float) -> str:
    """``2026-10-09T01:02:03Z`` for an epoch second."""
    return datetime.fromtimestamp(int(epoch_s), UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def default_clock(clock: ProtocolLabFillClock | None) -> ProtocolLabFillClock:
    return clock if clock is not None else SystemClock()
