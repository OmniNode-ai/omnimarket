# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelReplayProjection — deterministic projection of a replayed sequence.

Carries the two orthogonal deterministic signals the B6 canary gate
(OMN-14726) asserts on:

- ``projection_checksum`` — a checksum over the ordered fold of the sequence
  (content + ordering sensitive).
- ``cursor`` — the terminal delivery position (offset + count sensitive, order
  insensitive).

When the input supplied an expected result, the comparison fields report
whether the computed projection diverged and, if so, on which signal.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_delivery_replay_projection_compute.models.model_replay_cursor import (
    ModelReplayCursor,
)


class ModelReplayProjection(BaseModel):
    """Deterministic projection of a replayed delivery sequence.

    Attributes:
        correlation_id: Echo of the input ``correlation_id`` for tracing.
        projection_checksum: sha256 checksum over the ordered fold of the
            sequence. Identical for identical sequences; differs for any
            content or ordering divergence.
        cursor: Terminal delivery position (per-partition max offsets + count).
        event_count: Number of events in the replayed sequence.
        compared: Whether an expected result was supplied and compared.
        diverged: Whether the computed projection diverged from the expected
            result. Only meaningful when ``compared`` is True; ``False`` when no
            expectation was supplied.
        divergence_reasons: Which signals diverged — a subset of
            ``{"projection_checksum", "cursor"}``. Empty when not diverged.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID | None = Field(
        default=None, description="Echo of the input correlation_id for tracing."
    )
    projection_checksum: str = Field(
        ...,
        min_length=1,
        description="sha256 checksum over the ordered fold of the sequence.",
    )
    cursor: ModelReplayCursor = Field(
        ..., description="Terminal delivery position of the replayed sequence."
    )
    event_count: int = Field(
        default=0, ge=0, description="Number of events in the replayed sequence."
    )
    compared: bool = Field(
        default=False,
        description="Whether an expected result was supplied and compared.",
    )
    diverged: bool = Field(
        default=False,
        description="Whether the projection diverged from the expected result.",
    )
    divergence_reasons: tuple[str, ...] = Field(
        default=(),
        description="Signals that diverged (subset of projection_checksum, cursor).",
    )


__all__ = ["ModelReplayProjection"]
