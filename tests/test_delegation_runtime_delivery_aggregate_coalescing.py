# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The delegation writer's runtime delivery commits without waiting on the aggregate views.

MEASURED CAUSE (dev lane, 2026-11-09, runtime container on the dev host)

The group ``projection_delegation`` on ``delegate-skill-completed`` showed lag
episodes of 6.0, 32.4, 33.0, 5.1, 10.3, 4.2 and 4.6 s over 240 s at 50 ms polls,
one message in flight each time, with the handler raising nothing. Each
delivery reaches the runner through ``project_runtime_event``. That path never
calls ``run()``, which is the only place the OMN-22874 coalescer was enabled, so
every delegation write re-read the four whole-table aggregate views inline,
before its offset commit. The lab alarm's rule (lag grew across two consecutive
samples) reads any such episode as growth.

These tests drive ``project_runtime_event`` the way the runtime delivery host
does, with the aggregate read made slow by an injected delay, and read how long
one delivery holds its commit.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from omnimarket.models.model_message_meta import MessageMeta
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)

pytestmark = pytest.mark.unit

TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"
AGGREGATE_READ_S = 0.15
WINDOW_MS = 60


class _RuntimeHarness:
    """A runner fed through ``project_runtime_event``, its write instant and its view read slow."""

    def __init__(self, *, window_ms: int | None = WINDOW_MS) -> None:
        runner: Any = DelegationProjectionRunner(publish_fn=AsyncMock())
        if window_ms is not None:
            runner._coalescer.window_s = window_ms / 1000
        self.runner = runner
        runner._db = AsyncMock()
        runner._connect_standalone_databases = AsyncMock()
        runner._close_standalone_databases = AsyncMock()
        self.reads: list[float] = []
        self.published: list[str] = []
        self.fail_reads = 0
        self.t0 = time.monotonic()

        async def _execute(sql: str, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            if "snapshot_grain" in sql:
                self.reads.append(time.monotonic() - self.t0)
                await asyncio.sleep(AGGREGATE_READ_S)
                if self.fail_reads:
                    self.fail_reads -= 1
                    raise ConnectionError("injected: aggregate read failed")
                return [{"snapshot_grain": "g", "tenant_id": args[1]}]
            return []

        async def _publish(exposure: Any, **kwargs: Any) -> bool:
            self.published.append(exposure.topic)
            return True

        async def _project_delegate_skill_terminal(
            data: dict[str, Any], meta: Any
        ) -> bool:
            runner._delegation_writes += 1
            runner._last_delegation_write_tenant = TENANT
            return True

        runner._db.execute = _execute
        runner.publish_snapshot_delta = _publish
        runner._project_delegate_skill_terminal = _project_delegate_skill_terminal
        # The base class's producer stop is what the delivery's `finally` ends in.
        runner._producer = object()

    async def deliver(self, offset: int) -> float:
        """One runtime delivery; return how long it held its offset commit."""
        meta = MessageMeta(
            topic=self.runner._topic_delegate_skill_completed,
            partition=0,
            offset=offset,
            fallback_id=str(uuid4()),
        )
        started = time.monotonic()
        ok = await self.runner.project_runtime_event(
            self.runner._topic_delegate_skill_completed,
            {"correlation_id": str(uuid4())},
            meta,
        )
        assert ok is True
        return time.monotonic() - started

    def views(self) -> int:
        return len(self.runner._aggregate_exposures)


class TestTheRuntimeDeliveryNoLongerWaitsForTheViews:
    @pytest.mark.asyncio
    async def test_a_delivery_returns_before_one_aggregate_read_completes(self) -> None:
        h = _RuntimeHarness()
        try:
            held = await h.deliver(0)
            assert held < AGGREGATE_READ_S, (
                f"the delivery held its commit {held:.2f}s; one aggregate read "
                f"costs {AGGREGATE_READ_S:.2f}s, so the commit path still waits on the views"
            )
            assert h.reads == [], "an aggregate view was read inside the delivery"
        finally:
            await h.runner.close_runtime_resources()

    @pytest.mark.asyncio
    async def test_a_burst_republishes_each_view_once_after_the_window(self) -> None:
        h = _RuntimeHarness()
        try:
            for offset in range(3):
                await h.deliver(offset)
            deadline = time.monotonic() + 5
            while len(h.published) < h.views() and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert len(h.published) == h.views()
            assert len(h.reads) == h.views(), (
                f"{len(h.reads)} view reads for a burst of 3 deliveries; "
                f"expected one per view ({h.views()})"
            )
        finally:
            await h.runner.close_runtime_resources()

    @pytest.mark.asyncio
    async def test_closing_the_writer_publishes_what_the_window_still_holds(
        self,
    ) -> None:
        h = _RuntimeHarness(window_ms=5000)  # window far beyond the test
        await h.deliver(0)
        assert h.published == [], "nothing may publish inside the window"
        await h.runner.close_runtime_resources()
        assert len(h.published) == h.views(), "the close dropped the pending republish"

    @pytest.mark.asyncio
    async def test_the_producer_is_not_stopped_under_a_pending_republish(self) -> None:
        h = _RuntimeHarness(window_ms=5000)
        producer = h.runner._producer
        await h.deliver(0)
        assert h.runner._producer is producer, (
            "the delivery's own producer stop closed the producer the window's "
            "republish is about to use"
        )
        await h.runner.close_runtime_resources()


class TestTheErrorChain:
    @pytest.mark.asyncio
    async def test_a_failed_aggregate_read_never_holds_a_delivery_and_is_retried(
        self,
    ) -> None:
        h = _RuntimeHarness(window_ms=30)
        h.fail_reads = 1
        try:
            held = await h.deliver(0)
            assert held < AGGREGATE_READ_S, "a failing read held the delivery"
            deadline = time.monotonic() + 5
            while len(h.published) < h.views() and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            assert len(h.published) == h.views()
            assert len(h.reads) > h.views(), "the failed read was not retried"
        finally:
            await h.runner.close_runtime_resources()


class TestAContractWithoutAWindowKeepsTheInlineRepublish:
    @pytest.mark.asyncio
    async def test_no_declared_window_republishes_inside_the_delivery(self) -> None:
        h = _RuntimeHarness(window_ms=None)
        h.runner._coalescer.window_s = None
        held = await h.deliver(0)
        assert held >= h.views() * AGGREGATE_READ_S * 0.9
        assert len(h.published) == h.views()
        assert h.runner._coalescer.enabled is False
