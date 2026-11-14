# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fence of the ledger's authority handover (OMN-23154, rebuild plan 4.3 step 8, L13).

One fence guards both directions. ``admit`` counts an append as in flight from its admission to its
receipt and refuses it with ``FenceClosedError`` while a handover runs; ``handover`` closes
admission, waits until the in-flight count is zero, runs the switch with nothing in flight, changes
the authority and lifts the fence. A switch that raises, or a drain that does not finish in time,
leaves the authority as it was and lifts the fence, so a failed handover is a retry for the writers
and not a stuck ledger.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from omnimarket.models.work_ledger_append import EnumLedgerAuthority


class FenceClosedError(RuntimeError):
    """A handover is running: the command was not admitted and nothing was appended."""


class FenceDrainTimeoutError(TimeoutError):
    """An in-flight append did not finish within the declared drain wait."""


class FlipRefusedError(RuntimeError):
    """The state the handover would switch to does not match the state it leaves: nothing was switched."""


class HandoverInProgressError(RuntimeError):
    """A second handover was asked for while one runs."""


class LedgerFlipFence:
    def __init__(
        self,
        drain_wait_seconds: float,
        authority: EnumLedgerAuthority = EnumLedgerAuthority.FILE,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if drain_wait_seconds <= 0:
            raise ValueError("drain_wait_seconds must be positive")
        self._drain_wait_s = drain_wait_seconds
        self._clock = clock
        self._condition = threading.Condition()
        self._authority = authority
        self._closed = False
        self._in_flight = 0

    @property
    def authority(self) -> EnumLedgerAuthority:
        with self._condition:
            return self._authority

    @property
    def closed(self) -> bool:
        with self._condition:
            return self._closed

    @property
    def in_flight(self) -> int:
        with self._condition:
            return self._in_flight

    @contextmanager
    def admit(self) -> Iterator[EnumLedgerAuthority]:
        """Admit one append under the authority standing now; it is in flight until the block ends."""
        with self._condition:
            if self._closed:
                raise FenceClosedError("the ledger's authority handover is running")
            self._in_flight += 1
            authority = self._authority
        try:
            yield authority
        finally:
            with self._condition:
                self._in_flight -= 1
                self._condition.notify_all()

    def handover[T](self, to: EnumLedgerAuthority, switch: Callable[[], T]) -> T:
        """Fence admissions, drain, run ``switch`` with nothing in flight, then hold ``to`` as authority."""
        with self._condition:
            if self._closed:
                raise HandoverInProgressError("a handover is already running")
            self._closed = True
        try:
            with self._condition:
                deadline = self._clock() + self._drain_wait_s
                while self._in_flight:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        raise FenceDrainTimeoutError(
                            f"{self._in_flight} append(s) still in flight after {self._drain_wait_s}s"
                        )
                    self._condition.wait(remaining)
            result = switch()
            with self._condition:
                self._authority = to
            return result
        finally:
            with self._condition:
                self._closed = False
                self._condition.notify_all()


__all__: list[str] = [
    "FenceClosedError",
    "FenceDrainTimeoutError",
    "FlipRefusedError",
    "HandoverInProgressError",
    "LedgerFlipFence",
]
