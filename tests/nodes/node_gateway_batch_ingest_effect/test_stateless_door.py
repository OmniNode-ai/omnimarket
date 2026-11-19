# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC14: a stateless door: the same batch posted twice is written twice, nothing is kept between requests."""

from __future__ import annotations

import asyncio
import gc
import tracemalloc
from pathlib import Path

from omnimarket.nodes.node_gateway_batch_ingest_effect.models.model_batch_ingest import (
    ModelPublishOutcome,
    ModelWireRecord,
)
from tests.nodes.node_gateway_batch_ingest_effect.fakes import (
    batch,
    make_app,
    make_overlay,
    post,
    record,
)

NODE = (
    Path(__file__).parents[3]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_gateway_batch_ingest_effect"
)


def _three() -> bytes:
    return batch(record(value=b"1"), record(value=b"2"), record(value=b"3"))


async def test_stateless_door_same_batch_twice_is_published_twice_byte_identical() -> (
    None
):
    app, publisher = make_app()
    first = await post(app, _three())
    second = await post(app, _three())
    assert (first.status, first.body) == (202, {"published": 3})
    assert (second.status, second.body) == (202, {"published": 3})
    assert len(publisher.calls) == 6
    assert publisher.calls[:3] == publisher.calls[3:]
    assert [c.model_dump_json() for c in publisher.calls[:3]] == [
        c.model_dump_json() for c in publisher.calls[3:]
    ]


async def test_stateless_door_a_retry_after_a_healed_publisher_publishes_all_three_at_once() -> (
    None
):
    app, publisher = make_app()
    publisher.script = {0: "publish-refused"}
    failed = await post(app, _three())
    assert failed.status == 503
    assert failed.body["published"] == 0
    publisher.script = {}
    healed = await post(app, _three())
    assert (healed.status, healed.body) == (202, {"published": 3})
    assert len(publisher.calls) == 4  # one refused attempt, then all three at once


def test_stateless_door_has_no_store_claim_or_lease_module_or_key() -> None:
    names = {p.name for p in NODE.rglob("*.py")}
    assert not {
        n
        for n in names
        if any(w in n for w in ("store", "claim_", "lease", "reconcil"))
    }
    sources = "\n".join(p.read_text(encoding="utf-8") for p in NODE.rglob("*.py"))
    assert "sqlite" not in sources
    assert "lease" not in sources.lower().replace("release", "")
    assert "idempotency-key" in (NODE / "overlay.py").read_text(
        encoding="utf-8"
    )  # admitted as a header, never a key


class _Counting:
    message_max_bytes = 4096

    def __init__(self) -> None:
        self.count = 0

    async def publish(self, record: ModelWireRecord) -> ModelPublishOutcome:
        self.count += 1
        return ModelPublishOutcome(status="published")


async def test_stateless_door_memory_does_not_grow_over_1000_batches() -> None:
    publisher = _Counting()
    app, _ = make_app(
        make_overlay(read_idle_timeout_s=0.05, total_upload_deadline_s=0.1), publisher
    )  # type: ignore[arg-type]
    body = _three()
    for _ in range(100):
        await post(app, body)
    gc.collect()
    tracemalloc.start()
    baseline = tracemalloc.take_snapshot()
    for _ in range(1000):
        result = await post(app, body)
        assert result.status == 202
    await asyncio.sleep(
        0.3
    )  # let the loop drop the cancelled per-read timers, which are the loop's, not the door's
    gc.collect()
    growth = sum(
        s.size_diff
        for s in tracemalloc.take_snapshot().compare_to(baseline, "filename")
        if s.size_diff > 0
    )
    tracemalloc.stop()
    assert publisher.count == 3300
    assert growth < 200_000, growth
