# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fence of the serve's flip from the file to the bus (OMN-23154).

Every command enters through ``admit``. Once the handover has begun, ``admit`` hands a new command the
FENCED mode (the handler answers it with a retry receipt and appends nothing) and an append already
admitted counts as in flight until it leaves; ``wait_drained`` returns when the last of them has. The
mode changes only through ``begin_handover`` and ``settle``, so a command sees one mode for its whole run.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

from omnimarket.models.work_ledger_append import EnumLedgerAppendMode


class HandoverInProgressError(RuntimeError):
    """A handover began while another one is still running."""


class LedgerFlipFence:
    def __init__(self, mode: EnumLedgerAppendMode = EnumLedgerAppendMode.FILE) -> None:
        if mode is EnumLedgerAppendMode.FENCED:
            raise ValueError("a fence starts on the file or the bus, never fenced")
        self._cond = threading.Condition()
        self._mode: EnumLedgerAppendMode = mode
        self._in_flight = 0

    @property
    def mode(self) -> EnumLedgerAppendMode:
        with self._cond:
            return self._mode

    @property
    def in_flight(self) -> int:
        with self._cond:
            return self._in_flight

    @contextmanager
    def admit(self) -> Iterator[EnumLedgerAppendMode]:
        """The mode this command runs under; a command admitted outside FENCED is in flight until it leaves."""
        with self._cond:
            mode = self._mode
            if mode is not EnumLedgerAppendMode.FENCED:
                self._in_flight += 1
        if mode is EnumLedgerAppendMode.FENCED:
            yield mode
            return
        try:
            yield mode
        finally:
            with self._cond:
                self._in_flight -= 1
                self._cond.notify_all()

    def begin_handover(self) -> tuple[EnumLedgerAppendMode, int]:
        """Fence new commands; the mode before and the appends in flight at that moment."""
        with self._cond:
            if self._mode is EnumLedgerAppendMode.FENCED:
                raise HandoverInProgressError("a handover is already running")
            prior = self._mode
            self._mode = EnumLedgerAppendMode.FENCED
            return prior, self._in_flight

    def wait_drained(self, timeout_s: float) -> bool:
        """True when no admitted append is in flight, False when ``timeout_s`` passes first."""
        deadline = time.monotonic() + timeout_s
        with self._cond:
            while self._in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._cond.wait(remaining)
            return True

    def settle(self, mode: EnumLedgerAppendMode) -> None:
        """End the handover in ``mode`` (the new one, or the one it began from)."""
        if mode is EnumLedgerAppendMode.FENCED:
            raise ValueError("a handover settles on the file or the bus")
        with self._cond:
            self._mode = mode
            self._cond.notify_all()


__all__: list[str] = ["HandoverInProgressError", "LedgerFlipFence"]
