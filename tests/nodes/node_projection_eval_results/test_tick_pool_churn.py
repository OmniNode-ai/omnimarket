# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A tick inside the publish interval opens no database pool (OMN-26152).

The dev runtime delivers a runtime tick about once a second, and every delivery runs in its own
``asyncio.run`` loop. A pool belongs to the loop that opened it, so it cannot be kept across
deliveries; the cause of the churn (about 2,400 connect/close pairs per 30 minutes) is that the
delivery connected before it knew whether the tick owed any work. A tick inside the publish interval
owes none, so it must not connect.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from omnimarket.models.model_projection_request import ModelProjectionRequest
from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    TOPIC_TICK,
)
from omnimarket.projection.handler_shim import (
    INJECTED_ENVELOPE_TIMESTAMP_KEY,
    INJECTED_OFFSET_KEY,
    INJECTED_PARTITION_KEY,
    INJECTED_TENANT_ID_KEY,
    INJECTED_TOPIC_KEY,
)
from tests.nodes.node_projection_eval_results.support import (
    TENANT,
    FakeDb,
    MemoryStore,
    at,
    make_writer,
)

pytestmark = pytest.mark.unit


class CountingDb(FakeDb):
    def __init__(self) -> None:
        self.connects = 0
        self.closes = 0

    async def connect(self) -> None:
        self.connects += 1

    async def close(self) -> None:
        self.closes += 1


def _tick(now: datetime) -> ModelProjectionRequest:
    stamp = now.isoformat()
    return ModelProjectionRequest.from_injected(
        {
            "now": stamp,
            INJECTED_ENVELOPE_TIMESTAMP_KEY: stamp,
            INJECTED_TENANT_ID_KEY: TENANT,
            INJECTED_TOPIC_KEY: TOPIC_TICK,
            INJECTED_PARTITION_KEY: 0,
            INJECTED_OFFSET_KEY: 1,
        }
    )


def test_two_ticks_inside_the_interval_open_one_pool_in_total() -> None:
    writer, published = make_writer(MemoryStore())
    db = CountingDb()
    writer._db = db

    first = writer.project_request(_tick(at(5)))
    second = writer.project_request(_tick(at(5) + timedelta(seconds=1)))

    assert first["snapshot"]["published"] is True
    assert second["snapshot"] == {"published": False, "reason": "interval"}
    assert len(published) == 1
    assert (db.connects, db.closes) == (1, 1), "only the publishing tick opens a pool"


def test_a_hundred_ticks_inside_the_interval_open_no_pool() -> None:
    writer, _ = make_writer(MemoryStore())
    db = CountingDb()
    writer._db = db
    writer._last_published_at = at(5)

    for second in range(1, 101):
        writer.project_request(_tick(at(5) + timedelta(seconds=second)))

    assert (db.connects, db.closes) == (0, 0)
