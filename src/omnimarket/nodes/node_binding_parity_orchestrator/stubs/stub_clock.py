# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stub bindings of the clock protocol: the fixed reference and a clock that goes backward."""

from __future__ import annotations

from datetime import datetime, timedelta


class StubClockFixed:
    """Returns the run's fixed time on every read, so a replay stamps the same time."""

    def __init__(self, fixed_now: datetime) -> None:
        self._now = fixed_now

    def now(self) -> datetime:
        return self._now


class StubClockBackward:
    """Mutation: each read is earlier than the one before and the fixed time is ignored."""

    def __init__(self, fixed_now: datetime) -> None:
        self._now = fixed_now

    def now(self) -> datetime:
        self._now = self._now - timedelta(seconds=1)
        return self._now
