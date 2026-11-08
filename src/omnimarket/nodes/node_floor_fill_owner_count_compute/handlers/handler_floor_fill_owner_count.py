# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The owned and running counts of the stopped-line fill are the lanes that wrote in the window (OMN-22888).

On 2026-11-07T10:36Z the lab fill reported lane-resend running 321 of 393 and owned 56 of 96 red pull requests: it
counted every CLAIM never closed by a TERMINAL, however old. This handler counts a lane once when it wrote a row of
a liveness kind (CLAIM, TERMINAL) inside the overlay's window, and that one number is both counts.

The ledger is read through an injected reader and the time through an injected clock. The read runs under the floor's
emission contract: a start, then a success, or a typed failure when the ledger cannot be read, returned no row at all
(a silent ledger is not zero lanes), or no line could be parsed. An unknown count is never reported as zero.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

from omnimarket.handlers.emission_sink import (
    ChainEmitter,
    InMemoryEmissionSink,
    ProtocolEmissionSink,
)
from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_floor_fill_owner_count_compute.models import (
    ModelFloorFillOwnerCountOverlay,
    ModelFloorFillOwnerCountRequest,
    ModelFloorFillOwnerCountResult,
)
from omnimarket.nodes.node_floor_fill_owner_count_compute.protocols.protocol_lane_write_reader import (
    InMemoryLaneWriteReader,
    LaneWriteReadError,
    ProtocolLaneWriteReader,
)

NODE_NAME = "node_floor_fill_owner_count_compute"
FAILURE_UNREADABLE = "ledger_unreadable"
FAILURE_SILENT = "ledger_silent"
FAILURE_UNPARSEABLE = "ledger_unparseable"


class FloorFillOwnerCountError(RuntimeError):
    """The count could not be read; ``code`` is the typed failure code emitted on the chain."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code


def load_overlay(
    environ: dict[str, str] | None = None,
) -> ModelFloorFillOwnerCountOverlay:
    """The node overlay; a missing or broken one refuses naming the setting (no packaged value)."""
    return load_structured_overlay(NODE_NAME, ModelFloorFillOwnerCountOverlay, environ)


class HandlerFloorFillOwnerCount:
    """Count the lanes that wrote a liveness row in ``[as_of - window, as_of]``."""

    def __init__(
        self,
        overlay: ModelFloorFillOwnerCountOverlay,
        reader: ProtocolLaneWriteReader | None = None,
        clock: Callable[[], datetime] | None = None,
        sink: ProtocolEmissionSink | None = None,
    ) -> None:
        self._overlay = overlay
        self._reader = reader if reader is not None else InMemoryLaneWriteReader()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sink = sink if sink is not None else InMemoryEmissionSink()
        self._backend: Literal["in_memory", "real"] = (
            "in_memory" if isinstance(self._reader, InMemoryLaneWriteReader) else "real"
        )

    def handle(
        self, request: ModelFloorFillOwnerCountRequest
    ) -> ModelFloorFillOwnerCountResult:
        as_of = request.as_of if request.as_of is not None else self._clock()
        chain = ChainEmitter(
            self._sink,
            node=NODE_NAME,
            piece=self._overlay.emission_piece,
            clock=self._clock,
            backend=self._backend,
        )
        chain.start()
        window = self._overlay.lane_write_window_minutes
        try:
            writes = self._reader.read(as_of - timedelta(minutes=window), as_of)
        except LaneWriteReadError as exc:
            chain.failure(FAILURE_UNREADABLE, str(exc))
            raise FloorFillOwnerCountError(FAILURE_UNREADABLE, str(exc)) from None
        if writes.lines_read == 0 and not writes.rows:
            detail = f"no ledger row inside the last {window} minutes"
            if writes.lines_skipped:
                chain.failure(FAILURE_UNPARSEABLE, f"{writes.lines_skipped} lines")
                raise FloorFillOwnerCountError(
                    FAILURE_UNPARSEABLE, f"{writes.lines_skipped} lines skipped"
                )
            chain.failure(FAILURE_SILENT, detail)
            raise FloorFillOwnerCountError(FAILURE_SILENT, detail)
        kinds = set(self._overlay.liveness_kinds)
        lanes = tuple(sorted({row.lane for row in writes.rows if row.kind in kinds}))
        chain.success({"lanes": len(lanes), "lines_read": writes.lines_read})
        return ModelFloorFillOwnerCountResult(
            as_of=as_of,
            window_minutes=window,
            lanes=lanes,
            owned_count=len(lanes),
            running_count=len(lanes),
            lines_read=writes.lines_read,
            lines_skipped=writes.lines_skipped,
        )
