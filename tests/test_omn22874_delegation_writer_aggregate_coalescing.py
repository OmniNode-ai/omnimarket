# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22874: a burst of delegation events no longer leaves the writer's group in lag.

MEASURED CAUSE (lab dev lane, 2026-11-07, writer container on the dev host)

The delegation writer's consume loop republished four singleton aggregate views
(summary, model-routing, quality-gate, token-usage) INSIDE ``project_event``,
before the offset of the message that changed ``delegation_events`` was
committed. Each view is a whole-table aggregate over ``delegation_events``; read
with the live row count of 96,661 they cost about 2.2 s together, 1.9 s of it the
model-routing view. A burst of three ``delegate-skill-completed`` events showed
lag 1, 3, 3, 3, 3, 3, 2, 2, 2, 1, 1, 1 over eight seconds on the broker, one
commit about every 1.5 to 2 s, then 0. The lab alarm's rule (lag grew across two
consecutive samples) raised on every such burst: 25 raises in 24 hours.

What the reading rules out and what it does not: the loop commits per message,
so batch size is not the cause; and in the burst read above the commits advanced
one message at a time with no stall, which is the shape of a slow write, not of a
rebalance. It does not rule out a restart at a dev redeploy for other raises: the
writer container that ran earlier was recreated at 10:59:48Z, so its logs are
gone, and the broker's group log is behind SASL. Ledger rows of this alarm from
2026-11-05 and 2026-11-06 show a separate earlier regime of 17,382 to 20,153 lag
that this change does not address or explain.

THE FIX

The aggregates are a latest-wins compacted snapshot of a whole table. Publishing
one per message was never needed for correctness, only for freshness. In the
consume loop the republish is now marked dirty per tenant and flushed once per
contract-declared window, off the commit path. The runtime-dispatch path
(``project_runtime_event``) builds a runner per delivery and keeps the inline
republish.

These tests drive ``_handle_message`` the way the consume loop does, with the
aggregate read made slow by an injected delay, and read how long the writer
holds the burst uncommitted.
"""

from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)

pytestmark = pytest.mark.unit

TENANT_A = "820272f9-4aaf-5add-a2df-0af942852ab2"
TENANT_B = "a630ba90-7baa-4aec-b990-c34ec71ea2fb"

#: What one aggregate-view read costs in these tests. The lab measured 0.07 to
#: 1.9 s per view; the number here only has to be large against the commit work.
AGGREGATE_READ_S = 0.15
BURST = 3


class _Harness:
    """A runner whose write is instant and whose aggregate read is slow."""

    def __init__(self, *, coalesce: bool, window_ms: int = 60) -> None:
        self.runner = DelegationProjectionRunner(publish_fn=AsyncMock())
        self.runner._db = AsyncMock()  # type: ignore[assignment]
        self.events: list[tuple[str, float]] = []
        self.commit_times: list[float] = []
        self.published: list[dict[str, Any]] = []
        self.fail_reads = 0
        self.t0 = time.monotonic()
        self.tenant_for_next_write = TENANT_A

        async def _execute(sql: str, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            if "snapshot_grain" in sql:
                self.events.append(("aggregate-read", time.monotonic() - self.t0))
                await asyncio.sleep(AGGREGATE_READ_S)
                if self.fail_reads:
                    self.fail_reads -= 1
                    raise ConnectionError("injected: aggregate read failed")
                return [{"snapshot_grain": "g", "tenant_id": args[1]}]
            return []

        async def _publish(exposure: Any, **kwargs: Any) -> bool:
            self.published.append({"topic": exposure.topic, **kwargs})
            return True

        async def _project_delegate_skill_terminal(
            data: dict[str, Any], meta: Any
        ) -> bool:
            self.runner._delegation_writes += 1
            self.runner._last_delegation_write_tenant = self.tenant_for_next_write
            return True

        async def _noop_watermark(name: str, offset: int) -> None:
            return None

        async def _commit(offsets: dict[Any, int]) -> None:
            now = time.monotonic() - self.t0
            self.events.append(("commit", now))
            self.commit_times.append(now)

        self.runner._db.execute = _execute  # type: ignore[assignment]
        self.runner.publish_snapshot_delta = _publish  # type: ignore[assignment]
        self.runner._project_delegate_skill_terminal = (  # type: ignore[assignment]
            _project_delegate_skill_terminal
        )
        self.runner._update_watermark = _noop_watermark  # type: ignore[assignment]
        self.runner._consumer = SimpleNamespace(commit=_commit)  # type: ignore[assignment]
        if coalesce:
            self.runner._aggregate_coalesce_window_s = window_ms / 1000
            self.runner.enable_aggregate_coalescing()

    def message(self, offset: int) -> SimpleNamespace:
        correlation_id = str(uuid4())
        body = {
            "payload": {"correlation_id": correlation_id},
            "envelope_id": str(uuid4()),
            "correlation_id": correlation_id,
            "event_type": "omnimarket.delegate-skill-completed",
            "envelope_timestamp": "2026-11-07T09:30:00+00:00",
        }
        return SimpleNamespace(
            topic=self.runner._topic_delegate_skill_completed,
            partition=0,
            offset=offset,
            value=json.dumps(body).encode("utf-8"),
        )

    async def burst(self, n: int = BURST) -> float:
        """Feed ``n`` messages back to back; return when the last one committed."""
        self.t0 = time.monotonic()
        for offset in range(n):
            await self.runner._handle_message(self.message(offset))
        return self.commit_times[-1]

    def reads(self) -> int:
        return sum(1 for name, _ in self.events if name == "aggregate-read")


class TestTheCauseIsReproduced:
    """The control: the inline republish holds every commit behind the views."""

    @pytest.mark.asyncio
    async def test_inline_republish_holds_each_commit_behind_every_aggregate_read(
        self,
    ) -> None:
        h = _Harness(coalesce=False)
        views = len(h.runner._aggregate_exposures)
        assert views >= 2, "the contract declares no aggregate exposures to measure"

        last_commit = await h.burst()

        # Every message in the burst paid for every view before it committed.
        floor = BURST * views * AGGREGATE_READ_S
        assert last_commit >= floor * 0.9, (
            f"last commit at {last_commit:.2f}s, expected the inline republish to "
            f"hold it behind at least {floor:.2f}s of aggregate reads"
        )
        assert h.reads() == BURST * views


class TestTheBurstNoLongerWaitsForTheViews:
    @pytest.mark.asyncio
    async def test_a_burst_commits_without_waiting_for_one_aggregate_read(
        self,
    ) -> None:
        h = _Harness(coalesce=True)
        try:
            last_commit = await h.burst()
            assert last_commit < AGGREGATE_READ_S, (
                f"the burst was held {last_commit:.2f}s before its last commit; "
                f"one aggregate read costs {AGGREGATE_READ_S:.2f}s, so the commit "
                "path is still waiting on the views"
            )
        finally:
            await h.runner.drain_aggregates()

    @pytest.mark.asyncio
    async def test_the_burst_republishes_each_aggregate_once_not_once_per_message(
        self,
    ) -> None:
        h = _Harness(coalesce=True)
        views = len(h.runner._aggregate_exposures)
        await h.burst()
        await h.runner.drain_aggregates()

        assert h.reads() == views, (
            f"{h.reads()} aggregate reads for a burst of {BURST} messages; "
            f"expected one per view ({views})"
        )
        assert len(h.published) == views
        assert {p["tenant_id"] for p in h.published} == {TENANT_A}
        # Golden: the aggregate that is published is the one read AFTER the whole
        # burst was written, so the compacted topic ends on the final state.
        assert all(h.events[i][0] == "commit" for i in range(BURST))

    @pytest.mark.asyncio
    async def test_the_window_flushes_without_another_message(self) -> None:
        h = _Harness(coalesce=True, window_ms=40)
        views = len(h.runner._aggregate_exposures)
        await h.burst(1)
        # No drain, no further message: the trailing flush has to run by itself.
        await asyncio.sleep(0.04 + views * AGGREGATE_READ_S + 0.3)
        assert len(h.published) == views, (
            "a single event was never republished: the last message of a burst "
            "would leave the compacted aggregate stale until the next event"
        )

    @pytest.mark.asyncio
    async def test_each_tenant_in_the_window_is_republished_under_its_own_scope(
        self,
    ) -> None:
        h = _Harness(coalesce=True)
        views = len(h.runner._aggregate_exposures)
        h.tenant_for_next_write = TENANT_A
        await h.runner._handle_message(h.message(0))
        h.tenant_for_next_write = TENANT_B
        await h.runner._handle_message(h.message(1))
        await h.runner.drain_aggregates()

        assert len(h.published) == 2 * views
        by_tenant: dict[str, int] = {}
        for p in h.published:
            assert p["row"]["tenant_id"] == p["tenant_id"]
            by_tenant[p["tenant_id"]] = by_tenant.get(p["tenant_id"], 0) + 1
        assert by_tenant == {TENANT_A: views, TENANT_B: views}


class TestTheErrorChain:
    @pytest.mark.asyncio
    async def test_a_failed_aggregate_read_never_blocks_a_commit_and_is_retried(
        self,
    ) -> None:
        h = _Harness(coalesce=True, window_ms=30)
        views = len(h.runner._aggregate_exposures)
        h.fail_reads = 1  # the first view read raises

        last_commit = await h.burst()
        assert last_commit < AGGREGATE_READ_S, "a failing read held a commit"

        # No drain: the window itself retries the failed tenant until every
        # view is published.
        deadline = time.monotonic() + 5
        while len(h.published) < views and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert {p["topic"] for p in h.published} == {
            e.topic for e in h.runner._aggregate_exposures
        }
        assert h.reads() > views, "the failed read was not retried"
        await h.runner.drain_aggregates()

    @pytest.mark.asyncio
    async def test_drain_publishes_what_is_pending_and_then_nothing_is_pending(
        self,
    ) -> None:
        h = _Harness(coalesce=True, window_ms=5000)  # window far beyond the test
        views = len(h.runner._aggregate_exposures)
        await h.burst(2)
        assert h.published == [], "nothing may publish inside the window"

        await h.runner.drain_aggregates()
        assert len(h.published) == views

        await h.runner.drain_aggregates()
        assert len(h.published) == views, "a second drain republished again"


class TestTheRuntimePathStaysInline:
    @pytest.mark.asyncio
    async def test_without_enabling_coalescing_the_republish_is_inline(self) -> None:
        """``project_runtime_event`` builds a runner per delivery and tears it down
        when the delivery returns, so a deferred republish would be lost."""
        h = _Harness(coalesce=False)
        views = len(h.runner._aggregate_exposures)
        await h.runner._handle_message(h.message(0))
        assert len(h.published) == views


class TestTheWindowIsDeclaredNotHardCoded:
    def test_the_contract_declares_the_window(self) -> None:
        runner = DelegationProjectionRunner(publish_fn=AsyncMock())
        assert runner._aggregate_coalesce_window_s is not None
        assert runner._aggregate_coalesce_window_s > 0

    def test_enabling_without_a_declared_window_is_refused(self, tmp_path: Any) -> None:
        import yaml

        from omnimarket.nodes.node_projection_delegation.handlers import (
            handler_delegation,
        )

        src = handler_delegation.Path(handler_delegation.__file__).parent.parent
        contract = yaml.safe_load((src / "contract.yaml").read_text())
        contract.pop("aggregate_republish", None)
        path = tmp_path / "contract.yaml"
        path.write_text(yaml.safe_dump(contract))
        runner = DelegationProjectionRunner(path, publish_fn=AsyncMock())
        assert runner._aggregate_coalesce_window_s is None
        with pytest.raises(RuntimeError, match="aggregate_republish"):
            runner.enable_aggregate_coalescing()
