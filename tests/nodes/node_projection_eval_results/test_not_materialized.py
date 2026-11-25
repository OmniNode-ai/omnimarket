# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A judged event that meets an unmaterialized projection table is withheld a declared number of times, then dead-lettered.

On the dev runtime the refusal was rewound and redelivered without end (227 in 60 minutes). The
offset must still not advance while the write path may recover, so the node counts deliveries of the
same record, raises a typed refusal below the declared count and dead-letters the record with a typed
reason at the count. A dead letter that cannot be published is raised, never swallowed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnimarket.nodes.node_projection_eval_results.handlers.eval_results_deadline import (
    EvalResultsNotMaterializedError,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    TOPIC_JUDGED,
)
from omnimarket.projection.runner import MessageMeta
from tests.nodes.node_projection_eval_results.support import (
    MemoryStore,
    at,
    judged_event,
    make_writer,
)

pytestmark = pytest.mark.unit

CALL = "00000000-0000-0000-0000-0000000000c1"
DELIVERIES = 3


class UndefinedTableError(Exception):
    """The shape asyncpg raises for a relation that does not exist (SQLSTATE 42P01)."""

    sqlstate = "42P01"


class UnmaterializedStore(MemoryStore):
    async def load(self, plan: Any) -> Any:
        raise UndefinedTableError('relation "eval_item_scores" does not exist')


class BrokenStore(MemoryStore):
    async def load(self, plan: Any) -> Any:
        raise ValueError("a different failure")


def _meta(offset: int = 137196) -> MessageMeta:
    return MessageMeta(partition=0, offset=offset, fallback_id="", topic=TOPIC_JUDGED)


async def _deliver(writer: Any, offset: int = 137196) -> dict[str, Any]:
    payload = {
        **judged_event(CALL),
        "_envelope_timestamp": at(2).isoformat(),
        "_tenant_id": "11111111-1111-1111-1111-111111111111",
    }
    return await writer._project(TOPIC_JUDGED, payload, _meta(offset))


async def test_not_materialized_is_withheld_below_the_declared_count_then_dead_lettered_once() -> (
    None
):
    writer, published = make_writer(
        UnmaterializedStore(), not_materialized_max_deliveries=DELIVERIES
    )
    for _ in range(DELIVERIES - 1):
        with pytest.raises(EvalResultsNotMaterializedError):
            await _deliver(writer)
    outcome = await _deliver(writer)
    assert outcome["rows_upserted"] == 0
    assert outcome["dlq_routed"] == 1
    (refusal,) = outcome["refused"]
    assert refusal["reason"] == "not_materialized"
    dead = [(t, m) for t, m in published if t.startswith("onex.dlq.")]
    assert len(dead) == 1
    assert "not_materialized" in dead[0][1]["failure_reason"]
    assert dead[0][1]["retry_count"] == DELIVERIES


async def test_not_materialized_counts_each_record_apart_and_clears_a_record_that_recovers() -> (
    None
):
    store = UnmaterializedStore()
    writer, published = make_writer(store, not_materialized_max_deliveries=DELIVERIES)
    with pytest.raises(EvalResultsNotMaterializedError):
        await _deliver(writer, offset=1)
    with pytest.raises(EvalResultsNotMaterializedError):
        await _deliver(writer, offset=2)
    assert published == []
    healthy = MemoryStore()
    writer._store_factory = lambda _db: healthy
    await _deliver(writer, offset=1)
    writer._store_factory = lambda _db: store
    for _ in range(DELIVERIES - 1):
        with pytest.raises(EvalResultsNotMaterializedError):
            await _deliver(writer, offset=1)
    assert not [t for t, _ in published if t.startswith("onex.dlq.")]


async def test_not_materialized_a_dead_letter_that_cannot_be_published_is_raised_not_swallowed() -> (
    None
):
    writer, _ = make_writer(
        UnmaterializedStore(), not_materialized_max_deliveries=DELIVERIES
    )

    async def refusing(topic: str, value: bytes) -> None:
        raise RuntimeError("broker down")

    writer._publish_fn = refusing
    for _ in range(DELIVERIES - 1):
        with pytest.raises(EvalResultsNotMaterializedError):
            await _deliver(writer)
    with pytest.raises(
        EvalResultsNotMaterializedError, match="dead letter not published"
    ):
        await _deliver(writer)


async def test_not_materialized_a_dead_letter_publish_that_hangs_is_bounded() -> None:
    writer, _ = make_writer(
        UnmaterializedStore(),
        not_materialized_max_deliveries=1,
        stage_deadline_s=0.2,
        dispatch_deadline_s=1.0,
    )

    async def never(topic: str, value: bytes) -> None:
        await asyncio.Event().wait()

    writer._publish_fn = never
    with pytest.raises(
        EvalResultsNotMaterializedError, match="dead letter not published"
    ):
        await asyncio.wait_for(_deliver(writer), 5.0)


async def test_not_materialized_any_other_failure_is_not_counted_or_dead_lettered() -> (
    None
):
    writer, published = make_writer(BrokenStore(), not_materialized_max_deliveries=1)
    with pytest.raises(ValueError, match="a different failure"):
        await _deliver(writer)
    assert published == []
