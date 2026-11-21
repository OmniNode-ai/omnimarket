# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Names the call that holds an asyncio event loop.

Standard library only: this module is mounted into the kernel container under test and imported there as a top-level
module, so it imports nothing from omnimarket.

``asyncio.events.Handle._run`` is wrapped so each callback invocation gets a token and a start time; a callback (a task
step, a plain call_soon) is the unit a loop is held by. A sampler thread reads the running callback's token on a fixed
interval; while one callback has run longer than the bound it samples that thread's stack, so the stack comes from a
sampler and not from a post-hoc guess. When the callback returns, a record with its exact duration and the call the
samples saw most is handed to the sink. A callback still running past the bound is reported while it runs (the loop may
never come back), and again each further second.
"""

from __future__ import annotations

import asyncio.events
import collections
import itertools
import os
import sys
import threading
import time
from collections.abc import Callable
from types import FrameType
from typing import Any

_ASYNCIO_MARKER = os.sep + "asyncio" + os.sep


def _describe(frame: FrameType | None) -> list[str]:
    """User frames of a stack below the wrapped callback, outermost first, event-loop plumbing left out."""
    frames: list[str] = []
    while frame is not None:
        code = frame.f_code
        if code.co_filename == __file__ and code.co_name == "_run":
            break
        if _ASYNCIO_MARKER not in code.co_filename:
            frames.append(
                f"{os.path.basename(code.co_filename)}:{code.co_name}:{frame.f_lineno}"
            )
        frame = frame.f_back
    frames.reverse()
    return frames


class LoopHoldSampler:
    """Wrap Handle._run and sample the stack of any callback that outlives ``bound_seconds``."""

    def __init__(
        self,
        bound_seconds: float,
        interval_seconds: float,
        sink: Callable[[dict[str, Any]], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.bound_seconds = bound_seconds
        self.interval_seconds = interval_seconds
        self._sink = sink
        self._clock = clock
        self._tokens = itertools.count(1)
        self._running: dict[int, tuple[int, float]] = {}
        self._samples: dict[int, collections.Counter[tuple[str, ...]]] = {}
        self._reported_at: dict[int, float] = {}
        self._original: Any = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def install(self) -> None:
        if self._original is not None:
            return
        original = asyncio.events.Handle._run  # noqa: SLF001
        sampler = self

        def _run(handle: asyncio.events.Handle) -> None:
            ident = threading.get_ident()
            token = next(sampler._tokens)
            start = sampler._clock()
            sampler._running[ident] = (token, start)
            try:
                original(handle)
            finally:
                held = sampler._clock() - start
                sampler._running.pop(ident, None)
                if held > sampler.bound_seconds:
                    sampler._report(token, held, is_open=False)
                sampler._samples.pop(token, None)
                sampler._reported_at.pop(token, None)

        self._original = original
        asyncio.events.Handle._run = _run  # type: ignore[method-assign,assignment]  # noqa: SLF001
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._sample_loop, name="boot-loop-hold-sampler", daemon=True
        )
        self._thread.start()

    def uninstall(self) -> None:
        if self._original is None:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        asyncio.events.Handle._run = self._original  # type: ignore[method-assign]  # noqa: SLF001
        self._original = None
        self._thread = None

    def _sample_loop(self) -> None:
        while not self._stop.wait(self.interval_seconds):
            now = self._clock()
            for ident, (token, start) in list(self._running.items()):
                held = now - start
                if held <= self.bound_seconds:
                    continue
                frame = sys._current_frames().get(ident)  # noqa: SLF001
                stack = tuple(_describe(frame))
                if stack:
                    self._samples.setdefault(token, collections.Counter())[stack] += 1
                if now - self._reported_at.get(token, 0.0) >= 1.0:
                    self._reported_at[token] = now
                    self._report(token, held, is_open=True)

    def _report(self, token: int, held: float, *, is_open: bool) -> None:
        counter = self._samples.get(token)
        stack: tuple[str, ...] = counter.most_common(1)[0][0] if counter else ()
        self._sink(
            {
                "hold_id": f"{os.getpid()}:{token}",
                "held_seconds": round(held, 3),
                "call": " > ".join(stack) if stack else "unsampled",
                "stack": list(stack),
                "open": is_open,
            }
        )
