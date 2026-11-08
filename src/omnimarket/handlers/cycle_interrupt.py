# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A cycle that can say it was stopped (OMN-22864): termination signals and its own deadline end it as a typed failure.

Shared by every floor node that emits a start and must end it in a success or a typed failure. SIGKILL cannot be caught,
so each node sets its deadline below the time limit of the process that runs it.
"""

from __future__ import annotations

import signal
import threading
from collections.abc import Iterator
from contextlib import contextmanager


class CycleInterrupted(BaseException):
    """The cycle was stopped from outside (a termination signal, or its own deadline) and must say so before it exits.

    A ``BaseException`` so that no ``except Exception`` of a step swallows it; every ``start`` the cycle has emitted is
    closed with a typed failure on the way out.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@contextmanager
def interruptible(deadline_seconds: int) -> Iterator[None]:
    """Turn SIGTERM, SIGHUP and the deadline's SIGALRM into ``CycleInterrupted`` for the span of one cycle.

    Only the main thread can take signals; elsewhere (a test runner's worker thread) the span runs unguarded. SIGKILL
    cannot be caught: the deadline is set below the publish step's own time limit so that limit is never the one that
    ends a hung cycle.
    """
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def stopped(signum: int, _frame: object) -> None:
        raise CycleInterrupted(
            "terminated_by_signal", f"the cycle received {signal.Signals(signum).name}"
        )

    def overdue(_signum: int, _frame: object) -> None:
        raise CycleInterrupted(
            "cycle_deadline_exceeded",
            f"the cycle did not finish within {deadline_seconds} s",
        )

    previous = {
        signal.SIGTERM: signal.signal(signal.SIGTERM, stopped),
        signal.SIGHUP: signal.signal(signal.SIGHUP, stopped),
        signal.SIGALRM: signal.signal(signal.SIGALRM, overdue),
    }
    signal.setitimer(signal.ITIMER_REAL, deadline_seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        for signum, handler in previous.items():
            signal.signal(signum, handler)
