# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A run deadline for the J5 join, bounded inside the publish step's own time limit.

The J5 run is bounded inside the publish step's own time limit: a blocking call (a subprocess wait, a sleep) is
interrupted by the signal and the join closes its chain typed. The deadline is one shot: it arms a real-time itimer for
``seconds``, whose SIGALRM handler raises ``RunDeadlineExceededError``; the join's handler catches it and ends its open chain
with the deadline's typed failure. The timer is disarmed and the previous SIGALRM handler is restored when the block
ends, so the deadline never outlives the run it bounds.
"""

from __future__ import annotations

import contextlib
import signal
import threading
from collections.abc import Iterator

from omnimarket.nodes.node_work_ledger_append_effect.protocols.join_j5 import (
    RunDeadlineExceededError,
)


def _on_alarm(signum: int, frame: object) -> None:
    raise RunDeadlineExceededError(
        "the run's own time limit passed while a call was open"
    )


@contextlib.contextmanager
def run_deadline(seconds: float) -> Iterator[None]:
    """Arm a one-shot real-time itimer for ``seconds`` whose SIGALRM raises ``RunDeadlineExceededError``.

    ``seconds <= 0`` raises ``ValueError``. The deadline needs the main thread (a signal handler is only installed on the
    thread that installed it): a call from any other thread raises ``RuntimeError``. The timer is disarmed and the
    previous SIGALRM handler is restored when the block ends, on every path.
    """
    if seconds <= 0:
        raise ValueError(f"run_deadline needs a positive bound, got {seconds}")
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError(
            "run_deadline needs the main thread: a signal handler is only installed on that thread"
        )
    previous = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _on_alarm)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


__all__ = ["run_deadline"]
