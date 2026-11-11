# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The J0 join (piece 17, the floor, to piece 1, the code graph), run by the publish cycle (OMN-23022).

"The floor's record reaches the code graph." One run emits two chains under one correlation id, in the floor's emission
contract, so the receipt fold reads the join the way the floor does:

* piece 17's chain starts, then succeeds when the floor record reads and draws a reading age for the piece the code
  graph draws, and fails typed when it does not: ``floor_record_unreadable`` (the record is not a JSON object carrying
  the ``pieces`` list, an ISO-8601 ``generated_at`` and an int ``freshness_max_age_s``, or the file cannot be read),
  ``floor_record_stale`` (older than the record's own freshness bound) or ``code_graph_not_drawn`` (no row for the piece
  with an int reading age).
* piece 1's chain holds one typed ``upstream_`` refusal under the same id when piece 17 failed, the shape of a join's
  error case: the code graph refuses and does not start.

The control proves the reader breaks: the overlay's deliberately corrupt record text is fed to the same reader, and the
run fails when the reader accepts it.

Canonical definition-B shape: ``handle(ModelJoinJ0Command) -> ModelJoinJ0Result``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from omnimarket.handlers.emission_sink import (
    ChainEmitter,
    InMemoryEmissionSink,
    ProtocolEmissionSink,
)
from omnimarket.models.floor_17a import ModelJoinEnd
from omnimarket.nodes.node_join_j0_effect.models import (
    ModelFloorRecordFailure,
    ModelFloorRecordReading,
    ModelJoinJ0Command,
    ModelJoinJ0Result,
)
from omnimarket.nodes.node_join_j0_effect.overlay import (
    ModelJ0Join,
    ModelJoinJ0Overlay,
)

UPSTREAM_PREFIX = "upstream_"
FAIL_RECORD_UNREADABLE = "floor_record_unreadable"
FAIL_RECORD_STALE = "floor_record_stale"
FAIL_PIECE_NOT_DRAWN = "code_graph_not_drawn"


def read_floor_record(
    text: str, to_piece: int, now: datetime
) -> ModelFloorRecordReading | ModelFloorRecordFailure:
    """Judge the floor record's text for the piece the join draws: the reading it holds, or the typed failure that
    ends the piece-17 chain."""
    try:
        record: object = json.loads(text)
    except ValueError as exc:
        return ModelFloorRecordFailure(
            code=FAIL_RECORD_UNREADABLE,
            detail=f"{type(exc).__name__}: the floor record is not JSON",
        )
    if not isinstance(record, dict):
        return ModelFloorRecordFailure(
            code=FAIL_RECORD_UNREADABLE,
            detail="the floor record is not a JSON object",
        )
    pieces = record.get("pieces")
    if not isinstance(pieces, list):
        return ModelFloorRecordFailure(
            code=FAIL_RECORD_UNREADABLE,
            detail='the floor record has no "pieces" list',
        )
    max_age = record.get("freshness_max_age_s")
    if not isinstance(max_age, int) or isinstance(max_age, bool):
        return ModelFloorRecordFailure(
            code=FAIL_RECORD_UNREADABLE,
            detail='the floor record has no int "freshness_max_age_s"',
        )
    generated_at = record.get("generated_at")
    if not isinstance(generated_at, str):
        return ModelFloorRecordFailure(
            code=FAIL_RECORD_UNREADABLE,
            detail='the floor record has no ISO-8601 "generated_at"',
        )
    try:
        born = datetime.fromisoformat(generated_at)
    except ValueError:
        return ModelFloorRecordFailure(
            code=FAIL_RECORD_UNREADABLE,
            detail='the floor record\'s "generated_at" is not ISO-8601',
        )
    if born.tzinfo is None:
        born = born.replace(tzinfo=UTC)
    if (now - born).total_seconds() > max_age:
        return ModelFloorRecordFailure(
            code=FAIL_RECORD_STALE,
            detail=f"the record is older than its own bound of {max_age} s",
        )
    age = next(
        (
            p.get("reading_age_s")
            for p in pieces
            if isinstance(p, dict) and p.get("piece") == to_piece
        ),
        None,
    )
    if not isinstance(age, int) or isinstance(age, bool):
        return ModelFloorRecordFailure(
            code=FAIL_PIECE_NOT_DRAWN,
            detail=f"the floor record draws no reading age for piece {to_piece}",
        )
    return ModelFloorRecordReading(floor_rows=len(pieces))


class HandlerJoinJ0:
    """Run the J0 join once: emit its two chains, and prove the record reader breaks on the control's corrupt record."""

    def __init__(
        self,
        *,
        overlay: ModelJoinJ0Overlay,
        sink: ProtocolEmissionSink,
        floor_record_path: Path,
        backend: Literal["in_memory", "real"],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._overlay = overlay
        self._sink = sink
        self._floor_record_path = floor_record_path
        self._backend = backend
        self._clock = clock or (lambda: datetime.now(UTC))

    def handle(self, request: ModelJoinJ0Command) -> ModelJoinJ0Result:
        join = self._overlay.join
        now = self._clock()
        cid = uuid.uuid4().hex
        sink: ProtocolEmissionSink = (
            self._sink if request.write else InMemoryEmissionSink()
        )
        from_chain = self._chain(sink, join.from_node, join.from_piece, cid)
        from_chain.start()
        reading = self._read(join.to_piece, now)
        if isinstance(reading, ModelFloorRecordFailure):
            from_chain.failure(reading.code, reading.detail)
            self._chain(sink, join.to_node, join.to_piece, cid).refuse(
                f"{UPSTREAM_PREFIX}{reading.code}",
                f"{join.join}: piece {join.from_piece} failed with {reading.code}; "
                f"piece {join.to_piece} refuses and does not start",
            )
            return ModelJoinJ0Result(
                outcome="error",
                wrote=request.write,
                correlation_id=cid,
                failure_code=reading.code,
                detail=reading.detail,
            )
        from_chain.success({"floor_rows": reading.floor_rows})
        control, control_cid = self._control(request, join, sink)
        return ModelJoinJ0Result(
            outcome="golden",
            wrote=request.write,
            correlation_id=cid,
            floor_rows=reading.floor_rows,
            control=control,
            control_correlation_id=control_cid,
        )

    def _read(
        self, to_piece: int, now: datetime
    ) -> ModelFloorRecordReading | ModelFloorRecordFailure:
        try:
            text = self._floor_record_path.read_text(encoding="utf-8")
        except OSError as exc:
            return ModelFloorRecordFailure(
                code=FAIL_RECORD_UNREADABLE,
                detail=f"{type(exc).__name__}: the floor record could not be read",
            )
        return read_floor_record(text, to_piece, now)

    def _chain(
        self, sink: ProtocolEmissionSink, node: str, piece: int, cid: str
    ) -> ChainEmitter:
        return ChainEmitter(
            sink,
            node=node,
            piece=piece,
            clock=self._clock,
            correlation_id=cid,
            backend=self._backend,
        )

    def _control(
        self, request: ModelJoinJ0Command, join: ModelJ0Join, sink: ProtocolEmissionSink
    ) -> tuple[str, str]:
        """The join's deliberate fault on the piece-17 side: the deliberately corrupt record must be rejected by the
        same reader, or the control writes its two chains and the run says ``did_not_break``."""
        control_cid = "j0-control-" + uuid.uuid4().hex
        reading = read_floor_record(
            self._overlay.control.record_text, join.to_piece, self._clock()
        )
        if isinstance(reading, ModelFloorRecordReading):
            # The reader accepted the deliberately corrupt record: nothing is written for the control, and the run says so.
            return "did_not_break", control_cid
        from_chain = self._chain(sink, join.from_node, join.from_piece, control_cid)
        from_chain.start()
        from_chain.failure(reading.code, reading.detail)
        self._chain(sink, join.to_node, join.to_piece, control_cid).refuse(
            f"{UPSTREAM_PREFIX}{reading.code}",
            f"{join.join}: piece {join.from_piece} failed with {reading.code}; "
            f"piece {join.to_piece} refuses and does not start",
        )
        return "broke", control_cid


def refuse_unjudged_join_end(
    join_id: str,
    from_node: str,
    pipeline_chains: Sequence[str],
    join_ends: Sequence[ModelJoinEnd],
) -> None:
    """The join's from node must be a chain the floor reads and a join end the floor judges, or the floor would never
    read the chain the join writes under it (OMN-23022)."""
    if from_node not in pipeline_chains:
        raise ValueError(
            f"{from_node} is not among the chains the floor reads (pipeline_chains); "
            f"the floor would not read the chain join {join_id} writes under it"
        )
    if not any(end.join == join_id and end.node == from_node for end in join_ends):
        raise ValueError(
            f"join {join_id} has no join_end at {from_node}; the floor would not read "
            f"the chain the join writes under it"
        )
