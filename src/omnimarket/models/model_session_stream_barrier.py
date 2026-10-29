# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The session barrier test across the two capture streams (OMN-21123, TA.21).

The capture producer (``node_capture_item_identity_effect``) stamps every
record of a session with a per-session, per-stream ``producer_seq`` and journals
one barrier record per stream stating the highest number it has issued. These
are the read model's input and verdict: which sequence numbers each stream's
projection has applied, the barriers, and whether every number up to each
barrier's high-water has been applied.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

#: The two capture streams. The lineage stream is the hook-event metadata; the
#: content stream is the full content each lineage reference names.
SessionStream = Literal["lineage", "content"]


class ModelSessionStreamBarrier(BaseModel):
    """One barrier record, as the producer journalled it: ids, a count, a flag."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stream: SessionStream
    barrier_id: str = Field(min_length=1)
    high_water: int = Field(ge=0, strict=True)
    #: Set on the lineage stream's barrier only.
    content_capture: Literal["enabled", "disabled"] | None = None


class ModelSessionStreamBarrierProbe(BaseModel):
    """What a projection has applied for one session, and the barriers to test.

    ``applied`` maps a stream to the ``producer_seq`` values of the records its
    projection holds, in any order and with duplicates: a redelivered record is
    one record, so only distinct numbers count.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str = Field(min_length=1)
    barrier_id: str = Field(min_length=1)
    barriers: tuple[ModelSessionStreamBarrier, ...]
    applied: dict[SessionStream, tuple[int, ...]] = Field(default_factory=dict)
    #: The barrier's recorded source cutoff, distinct from its opaque identity.
    source_revision: str | None = Field(
        default=None, pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$"
    )
    #: The lane the session's lineage rows record, newest producer_seq first.
    lane: str | None = Field(default=None, min_length=1)


class ModelSessionStreamState(BaseModel):
    """One stream's standing against its barrier."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stream: SessionStream
    barrier_high_water: int | None
    #: The largest n such that every number 1..n has been applied.
    applied_high_water: int
    #: The lowest number at or below the barrier that has not been applied.
    first_missing: int | None
    met: bool


class ModelSessionStreamBarrierVerdict(BaseModel):
    """Whether the session barrier is met, stream by stream.

    ``outcome`` is ``met`` when every stream's applied high-water reached its
    barrier, ``not_met`` while a number up to a barrier is still missing,
    ``disabled`` when the lineage barrier says content capture was off, and
    ``no_barrier`` when the barrier id names no barrier for a stream.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    session_id: str
    barrier_id: str
    outcome: Literal["met", "not_met", "disabled", "no_barrier", "deadline"]
    streams: tuple[ModelSessionStreamState, ...]


__all__ = [
    "ModelSessionStreamBarrier",
    "ModelSessionStreamBarrierProbe",
    "ModelSessionStreamBarrierVerdict",
    "ModelSessionStreamState",
    "SessionStream",
]
