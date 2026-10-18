# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Test a session barrier against what the projections applied (OMN-21123, TA.21).

Shared by the two capture projections (``node_projection_session_content`` and
``node_projection_claude_hook_events``) and by the context sources effect that
waits on the barrier, so none of them imports another's handler.

A stream's applied high-water is the largest n such that every number 1..n has
been applied, counted over distinct numbers. So delay, reordering across or
within the streams, and duplicated records change nothing: the high-water
reaches a barrier only when no number up to it is missing. No clock, no broker,
no database.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Literal

from omnimarket.models.model_session_stream_barrier import (
    ModelSessionStreamBarrierProbe,
    ModelSessionStreamBarrierVerdict,
    ModelSessionStreamState,
    SessionStream,
)

STREAMS: tuple[SessionStream, ...] = ("lineage", "content")


def contiguous_high_water(applied: Iterable[int]) -> int:
    """The largest n with every number 1..n present; 0 when 1 is missing."""
    seen = {seq for seq in applied if seq >= 1}
    high_water = 0
    while high_water + 1 in seen:
        high_water += 1
    return high_water


def _stream_state(
    stream: SessionStream, barrier_high_water: int | None, applied: Iterable[int]
) -> ModelSessionStreamState:
    high_water = contiguous_high_water(applied)
    met = barrier_high_water is not None and high_water >= barrier_high_water
    first_missing = (
        high_water + 1
        if barrier_high_water is not None and high_water < barrier_high_water
        else None
    )
    return ModelSessionStreamState(
        stream=stream,
        barrier_high_water=barrier_high_water,
        applied_high_water=high_water,
        first_missing=first_missing,
        met=met,
    )


class HandlerSessionStreamBarrier:
    """The PURE barrier test: what was applied and the barriers in, a verdict out."""

    def handle(
        self, request: ModelSessionStreamBarrierProbe
    ) -> ModelSessionStreamBarrierVerdict:
        """Canonical entrypoint."""
        barriers = {
            barrier.stream: barrier
            for barrier in request.barriers
            if barrier.barrier_id == request.barrier_id
        }
        lineage = barriers.get("lineage")
        if lineage is not None and lineage.content_capture == "disabled":
            # Content capture was off: the content stream journalled nothing, so
            # only the lineage stream is tested, and the outcome says disabled.
            state = _stream_state(
                "lineage", lineage.high_water, request.applied.get("lineage", ())
            )
            return ModelSessionStreamBarrierVerdict(
                session_id=request.session_id,
                barrier_id=request.barrier_id,
                outcome="disabled",
                streams=(state,),
            )
        states = tuple(
            _stream_state(
                stream,
                barriers[stream].high_water if stream in barriers else None,
                request.applied.get(stream, ()),
            )
            for stream in STREAMS
        )
        outcome: Literal["met", "not_met", "no_barrier"]
        if any(state.barrier_high_water is None for state in states):
            outcome = "no_barrier"
        elif all(state.met for state in states):
            outcome = "met"
        else:
            outcome = "not_met"
        return ModelSessionStreamBarrierVerdict(
            session_id=request.session_id,
            barrier_id=request.barrier_id,
            outcome=outcome,
            streams=states,
        )


__all__ = ["STREAMS", "HandlerSessionStreamBarrier", "contiguous_high_water"]
