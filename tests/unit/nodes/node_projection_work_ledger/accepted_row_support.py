# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared builders for the accepted-row relay and republish tests (OMN-25630)."""

from __future__ import annotations

from collections.abc import Sequence

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.models.model_work_ledger_accepted_row import (
    EnumAcceptedRowSource,
    ModelAcceptedRowEvent,
)

LEDGER_ID = "rolling-work-ledger"


def row_text(n: int) -> str:
    minute, second = divmod(n, 60)
    return f"2026-09-28T10:{minute:02d}:{second:02d}Z | STATUS | lane=alpha | ticket=OMN-1 | step {n}"


def history_event(
    n: int, *, ordinal: int | None = None, seq: int | None = None
) -> ModelAcceptedRowEvent:
    raw = row_text(n)
    return ModelAcceptedRowEvent(
        ledger_id=LEDGER_ID,
        row_id=work_ledger_row_id(raw),
        raw_row=raw,
        source=EnumAcceptedRowSource.BOOTSTRAP,
        legacy_ordinal=ordinal,
        ledger_seq=seq,
    )


def live_event(n: int, seq: int) -> ModelAcceptedRowEvent:
    raw = row_text(n)
    return ModelAcceptedRowEvent(
        ledger_id=LEDGER_ID,
        row_id=work_ledger_row_id(raw),
        raw_row=raw,
        source=EnumAcceptedRowSource.LIVE,
        ledger_seq=seq,
    )


class PartitionCrashedError(RuntimeError):
    """The relay died between two publishes."""


class FakePartition:
    """One partition: a list, whose index is the offset. ``crash_after`` publishes, then it raises."""

    def __init__(self, crash_after: int | None = None, lossy: bool = False) -> None:
        self.events: list[ModelAcceptedRowEvent] = []
        self.crash_after = crash_after
        self.lossy = lossy  # a publish that returns but is not on the partition when it is read back

    def read(self) -> Sequence[ModelAcceptedRowEvent]:
        return tuple(self.events)

    def publish(self, event: ModelAcceptedRowEvent) -> None:
        if self.crash_after is not None and len(self.events) >= self.crash_after:
            raise PartitionCrashedError(f"crashed after {len(self.events)} publishes")
        if not self.lossy:
            self.events.append(event)


def no_live_before_a_bootstrap_event(events: Sequence[ModelAcceptedRowEvent]) -> bool:
    """True when every bootstrap event sits at a lower offset than every live event."""
    last_bootstrap = max(
        (
            i
            for i, e in enumerate(events)
            if e.source is EnumAcceptedRowSource.BOOTSTRAP
        ),
        default=-1,
    )
    first_live = min(
        (i for i, e in enumerate(events) if e.source is EnumAcceptedRowSource.LIVE),
        default=len(events),
    )
    return last_bootstrap < first_live
