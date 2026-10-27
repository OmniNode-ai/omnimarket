# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Keep the ledger writer consuming after the broker drops its group (OMN-20275).

After a dev broker restart the writer's aiokafka consumers logged "Heartbeat session expired - marking
coordinator dead" and "GroupCoordinatorNotAvailableError", and the host stopped appending until someone
ran ``launchctl kickstart``. ``WorkLedgerAppendHost.start`` takes its subscriptions once and nothing in
the serve process looked at them again, so recovery depended wholly on the bus client's own rejoin, which
waits for a measured backlog and a cooldown and has no reach into the host's subscriptions.

``ConsumeWatch`` probes on an interval whether the host's consumers hold partition assignments. After the
declared count of failed probes the host is DEGRADED: it logs, writes its state file, raises one alarm
on the exception queue for the episode and retakes its subscriptions under a growing, capped backoff
until the probe passes. The cadence, count and backoff are declared in the append node's contract
(``consume_watch``), not here.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.work_ledger_bus.bus import WORK_LEDGER_APPEND_NODE

logger = logging.getLogger(__name__)
ALARM_CLASS = "work-ledger-consume-degraded"

ConsumeProbe = Callable[[], Awaitable[str]]


class ModelConsumeWatchPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    probe_interval_seconds: float = Field(gt=0)
    unhealthy_probes_before_degraded: int = Field(ge=1)
    resubscribe_timeout_seconds: float = Field(gt=0)
    backoff_initial_seconds: float = Field(gt=0)
    backoff_factor: float = Field(ge=1)
    backoff_cap_seconds: float = Field(gt=0)


def load_consume_watch_policy() -> ModelConsumeWatchPolicy:
    contract = yaml.safe_load(
        resources.files(f"omnimarket.nodes.{WORK_LEDGER_APPEND_NODE}")
        .joinpath("contract.yaml")
        .read_text()
    )
    return ModelConsumeWatchPolicy.model_validate(contract["consume_watch"])


class ProtocolResubscribingHost(Protocol):
    async def resubscribe(self) -> None: ...


def readiness_probe(bus: object, topics: Callable[[], tuple[str, ...]]) -> ConsumeProbe:
    """A probe over the bus client's readiness view: '' when consuming, else the reason it is not.

    A topic that never held an assignment is not counted (the dev broker withholds some writer topics from
    this login); the host is unhealthy when a topic that once held one has lost it or its consume task has
    ended, or when no topic holds one at all. A bus without a readiness view (in-memory) is consuming.
    """
    seen: set[str] = set()

    async def probe() -> str:
        read = getattr(bus, "get_readiness_status", None)
        if read is None:
            return ""
        readiness = await read()
        wanted = topics()
        held = {
            topic
            for topic in wanted
            if readiness.assignments.get(topic)
            and readiness.consume_tasks_alive.get(topic, False)
        }
        lost = sorted(seen - held)
        seen.update(held)
        if lost:
            return f"lost assignment or consume task on {len(lost)} topic(s): {lost[0]}"
        if wanted and not held:
            return "no topic holds a partition assignment"
        return ""

    return probe


class ConsumeWatch:
    def __init__(
        self,
        host: ProtocolResubscribingHost,
        probe: ConsumeProbe,
        policy: ModelConsumeWatchPolicy,
        *,
        host_name: str,
        state_dir: Path,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._host = host
        self._probe = probe
        self._policy = policy
        self._host_name = host_name
        self._state_dir = state_dir
        self._clock = clock
        self._sleep = sleep
        self._now = now
        self._failed_probes = 0
        self._backoff = policy.backoff_initial_seconds
        self._next_attempt = 0.0
        self._task: asyncio.Task[None] | None = None
        self.degraded = False
        self.alarms: list[str] = []
        self.resubscribe_attempts = 0

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            await self._sleep(self._policy.probe_interval_seconds)
            try:
                await self.tick()
            except (
                Exception
            ):  # the watch is the recovery path; one failed sweep must not end it
                logger.exception("work-ledger consume watch: sweep failed")

    async def tick(self) -> None:
        try:
            reason = await self._probe()
        except Exception as exc:  # an unreadable probe is an unhealthy reading
            reason = f"probe failed: {type(exc).__name__}: {exc}"
        if not reason:
            self._healthy()
            return
        self._failed_probes += 1
        if self._failed_probes < self._policy.unhealthy_probes_before_degraded:
            return
        if not self.degraded:
            self._degrade(reason)
        if self._clock() < self._next_attempt:
            return
        self.resubscribe_attempts += 1
        try:
            await asyncio.wait_for(
                self._host.resubscribe(), self._policy.resubscribe_timeout_seconds
            )
        except (
            Exception
        ) as exc:  # the broker is still down; the next attempt follows the backoff
            logger.error(
                "work-ledger host %s: resubscribe attempt %d failed (%s: %s); next in %.0fs",
                self._host_name,
                self.resubscribe_attempts,
                type(exc).__name__,
                exc,
                self._backoff,
            )
        else:
            logger.warning(
                "work-ledger host %s: subscriptions retaken, waiting for assignments",
                self._host_name,
            )
        self._next_attempt = self._clock() + self._backoff
        self._backoff = min(
            self._backoff * self._policy.backoff_factor,
            self._policy.backoff_cap_seconds,
        )

    def _healthy(self) -> None:
        if self.degraded:
            logger.warning(
                "work-ledger host %s: consuming again after %d resubscribe attempt(s)",
                self._host_name,
                self.resubscribe_attempts,
            )
            self.degraded = False
            self._write_state("healthy", "")
        self._failed_probes = 0
        self.resubscribe_attempts = 0
        self._backoff = self._policy.backoff_initial_seconds
        self._next_attempt = 0.0

    def _degrade(self, reason: str) -> None:
        self.degraded = True
        logger.error("work-ledger host %s: DEGRADED, %s", self._host_name, reason)
        self._write_state("degraded", reason)
        self.alarms.append(self._raise_alarm(reason))

    def _write_state(self, state: str, reason: str) -> None:
        path = self._state_dir / "work-ledger-consume" / f"{self._host_name}.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "host": self._host_name,
                        "state": state,
                        "reason": reason,
                        "observed_at": self._now().isoformat(),
                    },
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
        except OSError:
            logger.warning(
                "work-ledger consume watch: state file not written", exc_info=True
            )

    def _raise_alarm(self, reason: str) -> str:
        """One decision row on the exception queue for the episode, the path the bus binding uses."""
        from omnimarket.nodes.node_exception_queue_effect.handler import (
            HandlerExceptionEmit,
        )
        from omnimarket.nodes.node_exception_queue_effect.models import (
            ModelExceptionEmitRequest,
            ModelExceptionRow,
        )

        result = HandlerExceptionEmit().handle(
            ModelExceptionEmitRequest(
                state_dir=self._state_dir / "exception-queue",
                row=ModelExceptionRow(
                    cls=ALARM_CLASS,
                    key=f"host={self._host_name}",
                    source="omnimarket.work_ledger_bus.consume_watch",
                    evidence=f"ledger writer is not consuming: {reason}",
                    tried="dropped and retook every subscription under the contract's backoff",
                    proposed_fix=(
                        "restore the durable broker; the writer resumes on its own once its "
                        "consumers are assigned partitions again"
                    ),
                    decision=True,
                    ticket="OMN-20275",
                ),
                now=self._now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        )
        return f"{result.status}:{result.id}"
