# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The gateway's declared budget of on-demand lane job-log fills per window."""

from __future__ import annotations

import fcntl
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path


class MissFillBudgetSpentError(Exception):
    """The window's declared number of on-demand fills is already spent."""


class MissFillBudget:
    """Counts a lane's on-demand job-log fills against the declared per-window budget."""

    def __init__(
        self,
        root: Path,
        *,
        max_fills: int,
        window_seconds: int,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.root = root
        self.max_fills = max_fills
        self.window_seconds = window_seconds
        self.clock = clock

    def spend(self) -> None:
        """Record one fill, refusing when the window's budget is spent."""
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        now = self.clock()
        log = self.root / "miss-fills.log"
        with (self.root / "miss-fills.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            stamps: list[float] = []
            if log.exists():
                for line in log.read_text().splitlines():
                    try:
                        stamp = float(line)
                    except ValueError:
                        continue
                    if now - stamp < self.window_seconds:
                        stamps.append(stamp)
            if len(stamps) >= self.max_fills:
                raise MissFillBudgetSpentError(
                    f"lane miss-fill budget spent: {self.max_fills} fills per "
                    f"{self.window_seconds} s"
                )
            stamps.append(now)
            fd, name = tempfile.mkstemp(dir=self.root, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as stream:
                    stream.write("\n".join(repr(s) for s in stamps) + "\n")
                Path(name).replace(log)
            finally:
                Path(name).unlink(missing_ok=True)
