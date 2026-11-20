# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-24171: every scoring keeps its whole trace; a trace that does not reassemble is refused."""

from __future__ import annotations

import base64

import pytest

from omnimarket.models.eval_item.model_eval_item_traced import (
    ModelEvalItemTracePart,
    ModelEvalTraceBound,
    assemble_trace,
)
from omnimarket.nodes.node_projection_eval_results.models.model_eval_projection_io import (
    TOPIC_TRACE,
)
from tests.nodes.node_projection_eval_results.support import (
    MemoryStore,
    at,
    deliver,
    make_writer,
    trace_parts,
)

pytestmark = pytest.mark.unit
RUN = "00000000-0000-0000-0000-0000000000a1"
_SMALL = ModelEvalTraceBound(
    broker_max_request_size=4096, envelope_headroom_bytes=1024, max_part_bytes=48
)


async def test_a_trace_in_several_parts_reassembles_byte_for_byte() -> None:
    store = MemoryStore()
    writer, _ = make_writer(store)
    parts = trace_parts(
        RUN,
        "t1",
        text="a task text long enough to need several parts " * 3,
        bound=_SMALL,
    )
    assert len(parts) > 2
    for part in reversed(parts):
        await deliver(writer, TOPIC_TRACE, part, at(3))
    stored = sorted(store.parts.values(), key=lambda p: p.part_index)
    assert len(stored) == len(parts)
    rebuilt = assemble_trace(
        tuple(
            ModelEvalItemTracePart.model_validate(
                {
                    "event_id": p.item_key,
                    "scoring_id": str(p.scoring_id),
                    "trace_sha256": p.trace_sha256,
                    "part_index": p.part_index,
                    "part_count": p.part_count,
                    "chunk_b64": p.chunk_b64,
                    "chunk_sha256": p.chunk_sha256,
                }
            )
            for p in stored
        )
    )
    assert rebuilt.input_text.startswith("a task text long enough")


async def test_two_scorings_of_one_item_at_one_time_keep_two_traces() -> None:
    store = MemoryStore()
    writer, _ = make_writer(store)
    first = trace_parts(RUN, "t1", text="first scoring", scored_at=at(3))
    second = trace_parts(RUN, "t1", text="second scoring", scored_at=at(3))
    assert first[0]["event_id"] == second[0]["event_id"]
    assert first[0]["scoring_id"] != second[0]["scoring_id"]
    for part in (*first, *second):
        await deliver(writer, TOPIC_TRACE, part, at(3))
    assert {str(p.scoring_id) for p in store.parts.values()} == {
        first[0]["scoring_id"],
        second[0]["scoring_id"],
    }


async def test_a_redelivered_part_changes_nothing() -> None:
    store = MemoryStore()
    writer, _ = make_writer(store)
    (part,) = trace_parts(RUN, "t1")
    await deliver(writer, TOPIC_TRACE, part, at(3))
    outcome = await deliver(writer, TOPIC_TRACE, part, at(9))
    assert outcome["traces_written"] == 0
    assert len(store.parts) == 1


async def test_a_trace_whose_parts_do_not_reassemble_is_refused_never_stored() -> None:
    store = MemoryStore()
    writer, _ = make_writer(store)
    parts = list(trace_parts(RUN, "t1", text="x" * 400, bound=_SMALL))
    assert len(parts) > 2
    # Alter one part's bytes consistently with its own chunk digest: the whole no longer matches its sha256.
    tampered = dict(parts[1])
    tampered["chunk_b64"] = base64.b64encode(b"tampered").decode()
    import hashlib

    tampered["chunk_sha256"] = hashlib.sha256(b"tampered").hexdigest()
    parts[1] = tampered
    outcomes = [await deliver(writer, TOPIC_TRACE, p, at(3)) for p in parts]
    assert any(
        o["refused"] and o["refused"][0]["reason"] == "trace_digest_mismatch"
        for o in outcomes
    )
    assert store.parts == {}
