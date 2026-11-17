# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared pieces of the runtime loop guard (OMN-24359 child): the overlay model, its loader and the loop-lag monitor.

The runtime calls a typed handler's ``handle`` on its event loop and awaits the result when it is awaitable, so a handler that
waits on a lab host's receipt without leaving the loop freezes every request topic. The monitor runs a heartbeat on the loop and
reports the longest gap between two wake-ups; the bound the gap is judged against comes from the guard's overlay.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable
from pathlib import Path
from types import TracebackType

import yaml
from pydantic import BaseModel, ConfigDict, Field

OVERLAY_PATH = Path(__file__).with_name("overlays") / "loop_guard.yaml"


class ModelLoopGuardOverlay(BaseModel):
    """The guard's settings, every value read from the overlay file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_loop_block_seconds: float = Field(gt=0)
    heartbeat_interval_seconds: float = Field(gt=0)
    block_factor: int = Field(gt=1)

    @property
    def block_seconds(self) -> float:
        """How long the injected blocking sender holds its thread."""
        return self.max_loop_block_seconds * self.block_factor


def load_loop_guard_overlay() -> ModelLoopGuardOverlay:
    return ModelLoopGuardOverlay.model_validate(
        yaml.safe_load(OVERLAY_PATH.read_text(encoding="utf-8"))
    )


class LoopLagMonitor:
    """A heartbeat on the running loop; ``max_gap`` is the longest time between two wake-ups beyond the interval."""

    def __init__(self, interval_seconds: float) -> None:
        self._interval = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._last = time.monotonic()
        self.max_gap = 0.0

    def _note(self) -> None:
        now = time.monotonic()
        self.max_gap = max(self.max_gap, now - self._last - self._interval)
        self._last = now

    async def _beat(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            self._note()

    async def __aenter__(self) -> LoopLagMonitor:
        self._last = time.monotonic()
        self._task = asyncio.create_task(self._beat())
        await asyncio.sleep(0)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        assert self._task is not None
        # A hold that ends without ever yielding leaves the heartbeat no turn to see it, so the exit reads the gap itself.
        self._note()
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task


async def longest_loop_gap[T](
    work: Awaitable[T], overlay: ModelLoopGuardOverlay
) -> tuple[T, float]:
    """Run ``work`` on the loop under the heartbeat; return its result and the longest gap seen."""
    async with LoopLagMonitor(overlay.heartbeat_interval_seconds) as monitor:
        result = await work
    return result, monitor.max_gap
