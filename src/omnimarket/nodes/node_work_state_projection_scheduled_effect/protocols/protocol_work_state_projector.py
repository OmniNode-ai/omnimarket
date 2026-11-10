# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The port the scheduled projection reads its projection through, and the in-memory default profile."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from omnimarket.models.work_state_projection import (
    EnumSourceReading,
    EnumWorkItemState,
    EnumWorkStateSource,
    ModelSourceStatus,
    ModelWorkStateResult,
)

NOT_PROJECTED = "in-memory profile: no source was read"


@runtime_checkable
class ProtocolWorkStateProjector(Protocol):
    """One projection for one cycle: reads the sources, projects, and writes where the floor and the fill read."""

    def project(self, now: datetime) -> ModelWorkStateResult: ...


@runtime_checkable
class ProtocolPublishingWorkStateProjector(ProtocolWorkStateProjector, Protocol):
    """A projector that also publishes the projection on the bus and says what the publication did."""

    publication: str


class UnreadWorkStateProjector:
    """The in-memory default profile: nothing was read, so every source is named unread and nothing is ready."""

    def project(self, now: datetime) -> ModelWorkStateResult:
        sources = tuple(EnumWorkStateSource)
        return ModelWorkStateResult(
            as_of=now,
            items=(),
            unread=tuple(f"{s.value}:{NOT_PROJECTED}" for s in sources),
            source_status=tuple(
                ModelSourceStatus(
                    source=s, reading=EnumSourceReading.UNREAD, cause=NOT_PROJECTED
                )
                for s in sources
            ),
            counts={state.value: 0 for state in EnumWorkItemState},
        )
