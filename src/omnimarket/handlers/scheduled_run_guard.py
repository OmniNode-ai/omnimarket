# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The second-run guard of a scheduled handler: whether a run is in flight, and since when."""

from __future__ import annotations

import threading
from datetime import datetime


class ScheduledRunGuard:
    """Holds the moment the in-flight run started, or None while the handler is idle.

    The start only clears when the run's own thread releases it, so a cancelled
    caller cannot free the guard while its worker is still running.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._since: datetime | None = None

    def try_acquire(
        self,
        now: datetime,
    ) -> float | None:
        """Take the guard for a new run, or report how long the in-flight run has held it.

        Returns None when the guard was idle and is now taken; otherwise the age of
        the run in flight in seconds (never negative), leaving the start where it was.
        """
        with self._lock:
            if self._since is None:
                self._since = now
                return None
            return max((now - self._since).total_seconds(), 0.0)

    def release(self) -> None:
        """End the run in flight, so the next acquire takes a fresh start."""
        with self._lock:
            self._since = None
