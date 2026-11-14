# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Concurrent deliveries do not replace or close each other's pool (OMN-21933).

The runtime calls the writer from worker threads, one message per call. A writer that
opened and closed the one shared adapter per message let delivery B replace the pool
delivery A was still writing through, then let A's cleanup close B's. The writer now
keeps one dispatch loop and one pool for its life, as the consumer flow writer does.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
import yaml

from omnimarket.nodes.node_projection_skill_lifecycle.handlers import (
    SkillLifecycleProjectionWriter,
)

pytestmark = pytest.mark.unit

_CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_skill_lifecycle/contract.yaml"
)
_COMPLETED_TOPIC = yaml.safe_load(_CONTRACT_PATH.read_text())["event_bus"][
    "subscribe_topics"
][1]
_DELIVERIES = 24
_WORKERS = 8


class _SlowPool:
    """Loop-bound adapter double: a statement yields, so deliveries interleave."""

    def __init__(self) -> None:
        self.connects = 0
        self.closes = 0
        self.loop: asyncio.AbstractEventLoop | None = None
        self.rows: list[Any] = []
        self._guard = threading.Lock()

    async def connect(self) -> None:
        with self._guard:
            self.connects += 1
        self.loop = asyncio.get_running_loop()

    async def close(self) -> None:
        with self._guard:
            self.closes += 1
        self.loop = None

    async def execute(
        self, query: str, *params: Any, tenant: str | None = None
    ) -> list[dict[str, Any]]:
        assert self.loop is asyncio.get_running_loop(), "pool replaced or closed"
        await asyncio.sleep(0.02)
        assert self.loop is asyncio.get_running_loop(), "pool replaced or closed"
        self.rows.append(params[0])
        return [{"event_id": params[0]}]


def _completed() -> dict[str, Any]:
    return {
        "event_id": str(uuid4()),
        "run_id": str(uuid4()),
        "skill_name": "ticket-pipeline",
        "repo_id": "omnimarket",
        "correlation_id": str(uuid4()),
        "status": "success",
        "duration_ms": 10,
        "emitted_at": "2026-11-02T07:00:02+00:00",
        "_topic": _COMPLETED_TOPIC,
    }


def test_concurrent_deliveries_share_one_pool_and_all_write() -> None:
    writer = SkillLifecycleProjectionWriter()
    pool = _SlowPool()
    writer._db = cast(Any, pool)
    try:
        with ThreadPoolExecutor(max_workers=_WORKERS) as threads:
            results = list(
                threads.map(writer.handle, [_completed() for _ in range(_DELIVERIES)])
            )
        assert [r["rows_upserted"] for r in results] == [1] * _DELIVERIES
        assert len(pool.rows) == _DELIVERIES
        assert pool.connects == 1
        assert pool.closes == 0
    finally:
        writer.close_dispatch()
    assert pool.closes == 1


def test_a_closed_writer_reopens_its_pool_for_the_next_delivery() -> None:
    writer = SkillLifecycleProjectionWriter()
    pool = _SlowPool()
    writer._db = cast(Any, pool)
    writer.handle(_completed())
    writer.close_dispatch()
    writer.handle(_completed())
    writer.close_dispatch()
    assert (pool.connects, pool.closes) == (2, 2)
