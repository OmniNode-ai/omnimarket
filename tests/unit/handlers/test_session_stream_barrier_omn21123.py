# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The session barrier test across the two capture streams (OMN-21123, TA.21).

Acceptance: the projection's high-water for a stream reaches a barrier's value
only when every sequence number up to it has been applied, under arrival orders
that reverse the two streams and with duplicated records -- falsifier: a
high-water at the barrier while a lower sequence number is missing.
"""

from __future__ import annotations

import itertools
import random

import pytest

from omnimarket.handlers.session_stream_barrier import (
    HandlerSessionStreamBarrier,
    contiguous_high_water,
)
from omnimarket.models.model_session_stream_barrier import (
    ModelSessionStreamBarrier,
    ModelSessionStreamBarrierProbe,
    ModelSessionStreamBarrierVerdict,
)
from omnimarket.nodes.node_capture_item_identity_effect.handlers.handler_item_identity import (
    build_barrier_payload,
)

pytestmark = pytest.mark.unit

SESSION = "11111111-2222-3333-4444-555555555555"
BARRIER = "barrier-1"
LINEAGE_HW = 6
CONTENT_HW = 4


def _barriers(
    *, content_capture_enabled: bool = True
) -> tuple[ModelSessionStreamBarrier, ...]:
    """The barriers exactly as the producer's builder writes them."""
    out = []
    for stream, high_water in (("lineage", LINEAGE_HW), ("content", CONTENT_HW)):
        payload = build_barrier_payload(
            session_id=SESSION,
            barrier_id=BARRIER,
            stream=stream,
            high_water_value=high_water,
            content_capture_enabled=content_capture_enabled,
            emitted_at="2026-10-18T00:00:00+00:00",
        )
        out.append(
            ModelSessionStreamBarrier.model_validate(
                {
                    k: payload[k]
                    for k in ("stream", "barrier_id", "high_water", "content_capture")
                    if k in payload
                }
            )
        )
    return tuple(out)


def _verdict(
    applied: dict[str, list[int]], **kwargs: bool
) -> ModelSessionStreamBarrierVerdict:
    probe = ModelSessionStreamBarrierProbe.model_validate(
        {
            "session_id": SESSION,
            "barrier_id": BARRIER,
            "barriers": _barriers(**kwargs),
            "applied": applied,
        }
    )
    return HandlerSessionStreamBarrier().handle(probe)


def _arrivals(order: str, *, duplicate: bool) -> list[tuple[str, int]]:
    """Every record of both streams, in an arrival order that interleaves them."""
    lineage = [("lineage", n) for n in range(1, LINEAGE_HW + 1)]
    content = [("content", n) for n in range(1, CONTENT_HW + 1)]
    if order == "lineage_first":
        records = lineage + content
    elif order == "content_first":
        records = content + lineage
    elif order == "both_reversed":
        records = list(reversed(content)) + list(reversed(lineage))
    else:
        records = lineage + content
        random.Random(order).shuffle(records)
    if duplicate:
        records = [r for record in records for r in (record, record)]
    return records


def test_contiguous_high_water_counts_only_an_unbroken_run_from_one() -> None:
    assert contiguous_high_water([]) == 0
    assert contiguous_high_water([2, 3]) == 0
    assert contiguous_high_water([3, 1, 2, 2, 1]) == 3
    assert contiguous_high_water([1, 2, 4, 5]) == 2


@pytest.mark.parametrize("duplicate", [False, True])
@pytest.mark.parametrize(
    "order", ["lineage_first", "content_first", "both_reversed", "seed-1", "seed-2"]
)
def test_the_barrier_is_met_only_once_every_number_has_arrived(
    order: str, duplicate: bool
) -> None:
    """Replay the arrivals one at a time; the barrier holds only at the very end."""
    applied: dict[str, list[int]] = {"lineage": [], "content": []}
    records = _arrivals(order, duplicate=duplicate)
    distinct_needed = LINEAGE_HW + CONTENT_HW
    for index, (stream, seq) in enumerate(records):
        applied[stream].append(seq)
        verdict = _verdict(applied)
        for state in verdict.streams:
            assert state.barrier_high_water is not None
            missing = set(range(1, state.barrier_high_water + 1)) - set(
                applied[state.stream]
            )
            # The falsifier: a high-water at the barrier while a lower number is missing.
            assert not (state.met and missing), (order, index, state)
            assert state.met == (not missing)
        have = len({(s, n) for s, n in records[: index + 1]})
        assert (verdict.outcome == "met") == (have == distinct_needed)
    assert verdict.outcome == "met"


@pytest.mark.parametrize(
    "dropped", list(itertools.product(["lineage", "content"], [1, 3]))
)
def test_a_missing_lower_number_holds_the_barrier_whatever_arrives_above_it(
    dropped: tuple[str, int],
) -> None:
    stream, seq = dropped
    applied = {
        "lineage": [n for n in range(1, LINEAGE_HW + 3) if ("lineage", n) != dropped],
        "content": [n for n in range(1, CONTENT_HW + 3) if ("content", n) != dropped],
    }
    verdict = _verdict(applied)
    assert verdict.outcome == "not_met"
    state = next(s for s in verdict.streams if s.stream == stream)
    assert not state.met
    assert state.applied_high_water == seq - 1
    assert state.first_missing == seq


def test_content_capture_disabled_tests_the_lineage_stream_only() -> None:
    verdict = _verdict(
        {"lineage": list(range(1, LINEAGE_HW + 1))}, content_capture_enabled=False
    )
    assert verdict.outcome == "disabled"
    assert [s.stream for s in verdict.streams] == ["lineage"]
    assert verdict.streams[0].met


def test_a_barrier_id_with_no_barrier_for_a_stream_is_not_met() -> None:
    probe = ModelSessionStreamBarrierProbe.model_validate(
        {
            "session_id": SESSION,
            "barrier_id": "another-barrier",
            "barriers": _barriers(),
            "applied": {"lineage": [1, 2, 3, 4, 5, 6], "content": [1, 2, 3, 4]},
        }
    )
    verdict = HandlerSessionStreamBarrier().handle(probe)
    assert verdict.outcome == "no_barrier"
    assert not any(state.met for state in verdict.streams)
