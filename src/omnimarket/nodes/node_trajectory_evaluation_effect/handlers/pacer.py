# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Per-process pacing for the evaluator (OMN-20087).

Over a rate bound the pacer waits (through the injected clock); over the daily
credit reservation it refuses; it never drops a command. The bounds are per
process and the daily reservation and credit stop are in memory, so a restart
resets them.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import yaml

from omnimarket.nodes.node_trajectory_evaluation_effect.models.model_trajectory_evaluation import (
    EnumTrajectoryEvaluationReason,
    ModelEvaluationPacing,
)
from omnimarket.nodes.node_trajectory_evaluation_effect.protocols import (
    ProtocolClock,
)

_CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"
_WINDOW_SECONDS = 60.0


def contract_pacing() -> ModelEvaluationPacing:
    data = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    return ModelEvaluationPacing.model_validate(data["evaluation_pacing"])


class _Window:
    """At most ``limit`` grants in any sliding 60 seconds."""

    def __init__(self, limit: int, clock: ProtocolClock) -> None:
        self._limit = limit
        self._clock = clock
        self._grants: deque[float] = deque()

    async def acquire(self) -> None:
        while True:
            now = self._clock.monotonic()
            while self._grants and self._grants[0] <= now - _WINDOW_SECONDS:
                self._grants.popleft()
            if len(self._grants) < self._limit:
                self._grants.append(now)
                return
            await self._clock.sleep(self._grants[0] + _WINDOW_SECONDS - now)


class Pacer:
    """Submit and status-read windows, an in-flight cap and a daily reservation."""

    def __init__(self, pacing: ModelEvaluationPacing, clock: ProtocolClock) -> None:
        self._pacing = pacing
        self._clock = clock
        self._submits = _Window(pacing.submits_per_minute, clock)
        self._reads = _Window(pacing.status_reads_per_minute, clock)
        self._in_flight = asyncio.Semaphore(pacing.max_in_flight)
        self._day: dt.date | None = None
        self._reserved = 0
        self._credit_stopped = False

    @asynccontextmanager
    async def submit_slot(self) -> AsyncIterator[None]:
        async with self._in_flight:
            await self._submits.acquire()
            yield

    async def acquire_status_read(self) -> None:
        await self._reads.acquire()

    def _roll_day(self) -> None:
        today = self._clock.utcnow().astimezone(dt.UTC).date()
        if today != self._day:
            self._day = today
            self._reserved = 0
            self._credit_stopped = False

    def _cost(self, content_mode: Literal["meta", "full"]) -> int:
        if content_mode == "full":
            return self._pacing.credits_per_full_text_command
        return self._pacing.credits_per_metadata_command

    def reserve(
        self, content_mode: Literal["meta", "full"]
    ) -> EnumTrajectoryEvaluationReason | None:
        """Take the daily reservation, or name why it was refused."""
        self._roll_day()
        cost = self._cost(content_mode)
        if cost == 0:
            return None
        if self._credit_stopped:
            return EnumTrajectoryEvaluationReason.CREDIT_STOP
        if self._reserved + cost > self._pacing.daily_reserved_credits:
            return EnumTrajectoryEvaluationReason.CREDIT_RESERVATION_EXHAUSTED
        self._reserved += cost
        return None

    def release(self, content_mode: Literal["meta", "full"]) -> None:
        """Give back a reservation whose submission was never accepted."""
        self._reserved = max(0, self._reserved - self._cost(content_mode))

    def trip_credit_stop(self) -> None:
        """Full-text commands are refused for the rest of the UTC day."""
        self._roll_day()
        self._credit_stopped = True
