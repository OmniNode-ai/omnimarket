# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-26410: the writer attests a designed zero-row tick and only that; a source event never carries it.

The dev runtime logged ``Projection handler wrote zero rows`` at ERROR about once a second
for this writer on the runtime tick topic: a tick returns ``rows_upserted: 0`` by design and
the runtime could not tell that from a lost write. The writer now says why, under
``projection_noop_reason``, and the runtime logs an attested no-op at INFO. The key and the
values are spelled as the wire strings the runtime reads, so this file reads the same before
the change (RED) and after it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    TOPIC_CODE_TRY,
    TOPIC_JUDGED,
    TOPIC_TICK,
)
from tests.nodes.node_projection_eval_results.support import (
    MemoryStore,
    at,
    code_event,
    deliver,
    judged_event,
    make_writer,
    overlay,
    recorded_judged_render_result,
    try_row,
)

pytestmark = pytest.mark.unit

NOOP_KEY = "projection_noop_reason"
CALL = "00000000-0000-0000-0000-00000000c001"
RUN = "00000000-0000-0000-0000-0000000000a1"


async def test_a_tick_inside_the_publish_interval_attests_a_tick_noop() -> None:
    store = MemoryStore()
    writer, _ = make_writer(store, clock=lambda: at(100))
    await deliver(writer, TOPIC_TICK, {"now": at(100).isoformat()}, at(100))
    outcome = await deliver(
        writer,
        TOPIC_TICK,
        {"now": (at(100) + timedelta(seconds=599)).isoformat()},
        at(110),
    )
    assert outcome["rows_upserted"] == 0
    assert outcome["snapshot"] == {"published": False, "reason": "interval"}
    assert outcome[NOOP_KEY] == "tick"


async def test_a_tick_that_republishes_attests_a_tick_noop() -> None:
    store = MemoryStore()
    writer, published = make_writer(store)
    outcome = await deliver(writer, TOPIC_TICK, {"now": at(5).isoformat()}, at(5))
    assert len(published) == 1
    assert outcome["rows_upserted"] == 0
    assert outcome["snapshot"]["published"] is True
    assert outcome[NOOP_KEY] == "tick"


async def test_a_tick_whose_snapshot_is_refused_is_not_attested_control() -> None:
    """A snapshot the reader is missing is not a designed no-op: the runtime keeps its ERROR."""
    store = MemoryStore()
    writer, published = make_writer(store, overlay_=overlay(snapshot_max_bytes=50))
    await deliver(writer, TOPIC_CODE_TRY, code_event(RUN, try_row(RUN, "t1")), at(2))
    outcome = await deliver(writer, TOPIC_TICK, {"now": at(5).isoformat()}, at(5))
    assert outcome["snapshot"]["published"] is False
    assert outcome["snapshot"]["refused"] == "over_bound"
    assert NOOP_KEY not in outcome
    assert published == []


async def test_a_valid_judged_delivery_eval_rows_upserted_one_and_is_not_attested() -> (
    None
):
    store = MemoryStore()
    writer, _ = make_writer(store)
    outcome = await deliver(writer, TOPIC_JUDGED, judged_event(CALL), at(2))
    assert outcome["rows_upserted"] >= 1
    assert len(store.items) == 1
    assert NOOP_KEY not in outcome


async def test_a_malformed_judged_delivery_dead_letters_and_is_not_attested_control() -> (
    None
):
    """The recorded dev broker event: dead-lettered, never a designed no-op."""
    store = MemoryStore()
    writer, _ = make_writer(store)
    outcome = await deliver(
        writer, TOPIC_JUDGED, recorded_judged_render_result(), at(2)
    )
    assert outcome["rows_upserted"] == 0
    assert outcome["dlq_routed"] == 1
    assert store.items == {}
    assert NOOP_KEY not in outcome
