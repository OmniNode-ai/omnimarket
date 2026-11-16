# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24914: the close fold keeps one winner; a repeated trace part refuses only itself."""

from __future__ import annotations

import base64
import hashlib
from uuid import uuid4

import pytest

from omnimarket.models.eval_item.model_eval_item_traced import (
    ModelEvalItemTracePart,
    ModelEvalTraceBound,
    assemble_trace,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    TOPIC_CODE_TRY,
    TOPIC_RUN_CLOSED,
    TOPIC_TRACE,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_rows import (
    EnumEvalRunState,
)
from tests.nodes.node_projection_eval_results.support import (
    MODEL,
    MemoryStore,
    at,
    close_event,
    code_event,
    deliver,
    make_writer,
    trace_parts,
    try_row,
)

pytestmark = pytest.mark.unit
RUN = "00000000-0000-0000-0000-0000000000a1"
_SEQUENCES = (("t1", MODEL, "S"),)
_SMALL = ModelEvalTraceBound(
    broker_max_request_size=4096, envelope_headroom_bytes=1024, max_part_bytes=48
)


async def _items(writer: object) -> None:
    await deliver(writer, TOPIC_CODE_TRY, code_event(RUN, try_row(RUN, "t1")), at(2))


async def test_an_older_close_cannot_rewrite_the_rates_from_the_losing_record_in_either_order() -> (
    None
):
    """The newer close reads closed; an older one that says void must not turn the rates void."""
    newer = close_event(RUN, _SEQUENCES, closed_at=at(30))
    older = close_event(RUN, _SEQUENCES, closed_at=at(10), void_reason="stale_run")
    tables = []
    for order in ((newer, older), (older, newer)):
        store = MemoryStore()
        writer, _ = make_writer(store)
        await _items(writer)
        for payload in order:
            await deliver(writer, TOPIC_RUN_CLOSED, payload, at(40))
        assert {r.run_state for r in store.rates} == {EnumEvalRunState.CLOSED}
        assert {r.void_reason for r in store.rates} == {""}
        (record,) = store.runs.values()
        assert record.closed_at == at(30)
        tables.append(store.tables())
    assert tables[0] == tables[1]


async def test_an_equal_time_close_is_decided_by_digest_in_either_order() -> None:
    """Two closes at one time that differ in the void cause leave the same record and rates."""
    a = close_event(RUN, _SEQUENCES, closed_at=at(30))
    b = close_event(RUN, _SEQUENCES, closed_at=at(30), void_reason="stale_run")
    tables = []
    for order in ((a, b), (b, a)):
        store = MemoryStore()
        writer, _ = make_writer(store)
        await _items(writer)
        for payload in order:
            await deliver(writer, TOPIC_RUN_CLOSED, payload, at(40))
        (record,) = store.runs.values()
        # The rates are the recount from the record that stands, whichever arrived last.
        assert {r.void_reason for r in store.rates} == {record.void_reason}
        tables.append(store.tables())
    assert tables[0] == tables[1]


async def _verified_trace(writer: object, store: MemoryStore) -> tuple[dict, ...]:
    parts = trace_parts(RUN, "t1", text="x" * 400, bound=_SMALL)
    assert len(parts) > 2
    for p in parts:
        await deliver(writer, TOPIC_TRACE, p, at(3))
    assert len(store.parts) == len(parts)
    return parts


async def test_a_repeated_part_with_different_bytes_refuses_only_that_part_of_a_verified_trace() -> (
    None
):
    store = MemoryStore()
    writer, _ = make_writer(store)
    parts = await _verified_trace(writer, store)
    before = dict(store.parts)
    tampered = dict(parts[1])
    tampered["chunk_b64"] = base64.b64encode(b"tampered").decode()
    tampered["chunk_sha256"] = hashlib.sha256(b"tampered").hexdigest()
    outcome = await deliver(writer, TOPIC_TRACE, tampered, at(9))
    assert [r["reason"] for r in outcome["refused"]] == ["trace_digest_mismatch"]
    assert outcome["dlq_routed"] == 1
    assert store.parts == before, "a stored part was deleted or replaced"
    rows = sorted(store.parts.values(), key=lambda r: r.part_index)
    assert len(rows) == len(parts)
    assert assemble_trace(
        tuple(ModelEvalItemTracePart.model_validate(p) for p in parts)
    )


async def test_a_repeated_part_with_different_bytes_in_a_partial_trace_keeps_the_held_parts() -> (
    None
):
    store = MemoryStore()
    writer, _ = make_writer(store)
    parts = trace_parts(RUN, "t1", text="x" * 400, bound=_SMALL)
    await deliver(writer, TOPIC_TRACE, parts[0], at(3))
    await deliver(writer, TOPIC_TRACE, parts[1], at(3))
    tampered = dict(parts[0])
    tampered["chunk_b64"] = base64.b64encode(b"tampered").decode()
    tampered["chunk_sha256"] = hashlib.sha256(b"tampered").hexdigest()
    outcome = await deliver(writer, TOPIC_TRACE, tampered, at(4))
    assert [r["reason"] for r in outcome["refused"]] == ["trace_digest_mismatch"]
    assert sorted(p.part_index for p in store.parts.values()) == [0, 1]


async def test_a_part_of_another_item_under_the_same_scoring_is_refused_by_the_agree_check() -> (
    None
):
    """Held parts are rebuilt with their own stored item key, so a part naming another item disagrees."""
    store = MemoryStore()
    writer, _ = make_writer(store)
    parts = trace_parts(RUN, "t1", text="x" * 400, bound=_SMALL)
    await deliver(writer, TOPIC_TRACE, parts[0], at(3))
    stranger = {**parts[1], "event_id": str(uuid4())}
    outcome = await deliver(writer, TOPIC_TRACE, stranger, at(4))
    assert [r["reason"] for r in outcome["refused"]] == ["trace_digest_mismatch"]
    assert "event_id" in outcome["refused"][0]["detail"]
    assert sorted(p.part_index for p in store.parts.values()) == [0]
