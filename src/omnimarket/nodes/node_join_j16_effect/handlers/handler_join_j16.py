# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The J16 join (rebuild plan section 4.1, piece 16 to piece 17), run by the publish cycle (OMN-23122).

"An eval result appears on the floor." One run emits two chains under one correlation id, in the floor's emission contract,
so the receipt fold reads the join the way the floor does:

* piece 16's chain starts, then succeeds when the newest reading row of piece 16 on the ledger holds an eval result that
  is inside its own bound, and fails typed when it does not: ``eval_reading_absent`` (no reading row), ``eval_run_without_result``
  (the writer's verdict is UNKNOWN: no eval run ended in a result) or ``eval_result_stale`` (older than the row's max age).
  A result whose own verdict is NOT WORKING is still a result: the join tests that a result reaches the floor, and the
  piece's verdict is drawn by the piece, not decided here.
* piece 17's chain starts after that success and ends when the floor record shows piece 16's row with a reading age
  (success) or does not (``eval_result_not_drawn``, ``floor_record_unreadable``). When piece 16 failed, piece 17 never
  starts: it holds one typed ``upstream_`` refusal under the same id, the shape of a join's error case.

Nothing here reads a missing result as a success. An unreadable ledger writes nothing and the run says ``unread``.

Canonical definition-B shape: ``handle(ModelJoinJ16Command) -> ModelJoinJ16Result``.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from omnimarket.handlers.emission_sink import (
    ChainEmitter,
    InMemoryEmissionSink,
    ProtocolEmissionSink,
)
from omnimarket.models.floor_17a import ModelReadingRow
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    reading_rows,
)
from omnimarket.nodes.node_join_j16_effect.models import (
    ModelJoinJ16Command,
    ModelJoinJ16Result,
)
from omnimarket.nodes.node_join_j16_effect.overlay import ModelJoinJ16Overlay
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
)

UPSTREAM_PREFIX = "upstream_"
FAIL_NO_READING = "eval_reading_absent"
FAIL_NO_RESULT = "eval_run_without_result"
FAIL_STALE = "eval_result_stale"
FAIL_NOT_DRAWN = "eval_result_not_drawn"
FAIL_RECORD_UNREADABLE = "floor_record_unreadable"
_FUTURE_SLACK = timedelta(days=1)


class HandlerJoinJ16:
    """Run the J16 join once and emit its two chains."""

    def __init__(
        self,
        *,
        overlay: ModelJoinJ16Overlay,
        source: ProtocolLedgerRowSource,
        sink: ProtocolEmissionSink,
        floor_record_path: Path,
        backend: Literal["in_memory", "real"],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._overlay = overlay
        self._source = source
        self._sink = sink
        self._floor_record_path = floor_record_path
        self._backend = backend
        self._clock = clock or (lambda: datetime.now(UTC))

    def handle(self, request: ModelJoinJ16Command) -> ModelJoinJ16Result:
        join = self._overlay.join
        now = self._clock()
        cid = uuid.uuid4().hex
        read = self._source.read(
            ModelLedgerQuery(
                since=now - timedelta(days=join.reading.lookback_days),
                until=now + _FUTURE_SLACK,
                row_type=join.reading.row_type,
                lane=join.reading.lane,
            )
        )
        if read.error is not None:
            # An unreadable ledger is never an empty answer: nothing is written, and the run says so.
            return ModelJoinJ16Result(
                outcome="unread",
                wrote=False,
                correlation_id=cid,
                detail=f"ledger unread: {read.error}",
            )
        readings, _rejected = reading_rows(list(read.rows), join.reading)
        reading = next((r for r in readings if r.piece == join.from_piece), None)
        sink: ProtocolEmissionSink = (
            self._sink if request.write else InMemoryEmissionSink()
        )
        upstream_code, upstream_detail = self._eval_failure(reading, now)
        eval_chain = self._chain(sink, join.from_node, join.from_piece, cid)
        eval_chain.start()
        if upstream_code:
            eval_chain.failure(upstream_code, upstream_detail)
            self._chain(sink, join.to_node, join.to_piece, cid).refuse(
                f"{UPSTREAM_PREFIX}{upstream_code}",
                f"{join.join}: piece {join.from_piece} failed with {upstream_code}; "
                f"piece {join.to_piece} refuses and does not start",
            )
            return ModelJoinJ16Result(
                outcome="error",
                wrote=request.write,
                correlation_id=cid,
                failed_side="from",
                failure_code=upstream_code,
                detail=upstream_detail,
                reading_age_s=_age_s(reading, now),
            )
        eval_chain.success({"eval_results": 1})
        floor_chain = self._chain(sink, join.to_node, join.to_piece, cid)
        floor_chain.start()
        drawn_age, drawn_code, drawn_detail = self._drawn(join.from_piece)
        if drawn_code:
            floor_chain.failure(drawn_code, drawn_detail)
            return ModelJoinJ16Result(
                outcome="error",
                wrote=request.write,
                correlation_id=cid,
                failed_side="to",
                failure_code=drawn_code,
                detail=drawn_detail,
                reading_age_s=_age_s(reading, now),
            )
        floor_chain.success({"eval_results_drawn": 1})
        return ModelJoinJ16Result(
            outcome="golden",
            wrote=request.write,
            correlation_id=cid,
            reading_age_s=drawn_age,
        )

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

    @staticmethod
    def _eval_failure(
        reading: ModelReadingRow | None, now: datetime
    ) -> tuple[str, str]:
        """The typed failure of piece 16's chain, or ``("", "")`` when the ledger holds a fresh eval result."""
        if reading is None:
            return FAIL_NO_READING, "no reading row of the eval piece on the ledger"
        if reading.state == "UNKNOWN":
            why = reading.reason or "no reason given"
            return FAIL_NO_RESULT, f"the writer's verdict is UNKNOWN: {why}"[:300]
        age = (now - reading.probed_at).total_seconds()
        if age > reading.max_age_s:
            return (
                FAIL_STALE,
                f"the result is {int(age)} s old, bound {reading.max_age_s} s",
            )
        return "", ""

    def _drawn(self, piece: int) -> tuple[int | None, str, str]:
        """The age the floor record shows on the eval piece's row, or the typed failure of reading it."""
        try:
            record: Any = json.loads(
                self._floor_record_path.read_text(encoding="utf-8")
            )
            rows = record["pieces"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return (
                None,
                FAIL_RECORD_UNREADABLE,
                f"{type(exc).__name__}: the floor record could not be read",
            )
        row = next(
            (p for p in rows if isinstance(p, dict) and p.get("piece") == piece), None
        )
        age = row.get("reading_age_s") if row else None
        if isinstance(age, int) and not isinstance(age, bool):
            return age, "", ""
        return (
            None,
            FAIL_NOT_DRAWN,
            "the floor record holds no reading age for the eval piece",
        )


def _age_s(reading: ModelReadingRow | None, now: datetime) -> int | None:
    return None if reading is None else int((now - reading.probed_at).total_seconds())


def refuse_unread_floor_node(to_node: str, floor_chains: Sequence[str]) -> None:
    """A join's target node must be one of the floor's declared piece chains, or the join's own
    refusal is a record the floor never reads (OMN-23601)."""
    if to_node not in floor_chains:
        raise ValueError(
            f"{to_node} is not among the chains the floor reads for its piece "
            f"(pipeline_chains); the join's refusal would be unread and read as "
            f"downstream_missing_typed_failure"
        )
