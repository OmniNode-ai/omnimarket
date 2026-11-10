# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J6 run (OMN-22936): answer each piece 6 golden reading with a stop in the gate store, audited and fenced.

Canonical shape: ``handle(ModelJoinJ6RunCommand) -> ModelJoinJ6RunResult``. The join handler (``HandlerGateStoreJoinJ6``)
judges one stop and one send; nothing called it, so the floor read J6 ``not_run``. This is the caller. Piece 6's golden
chains are the floor's own: the ledger rows of its event rule, under the correlation id the floor gives each row. For each
such chain the journal does not show piece 7 answering, this run commits one stop on a test piece in the gate store, reads
the durable audit row and the projected row back for that piece, checks that the next send of the stopped piece is refused
by the store and that a send of another test piece is admitted, and writes piece 7's start and success (or a typed failure)
to the journal under that chain's id. A chain piece 7 already answered is not answered again, so a rerun stops nothing.

The fence drill is piece 7's own error chain: a stop whose commit gets no answer by its deadline must leave the send of that
piece refused (the fence fails closed) while another piece is admitted. When the drill holds it is written once per window as
a drill chain ending in a typed failure; when it does not hold, nothing is written and the result says the drill did not
hold, so the floor never reads an error receipt the fence did not earn.

An unreadable ledger is never an empty answer: nothing is owed or written, and the result says so. A probe reads what is
owed and writes nothing. The gate store here is the in-memory twin; chains are written with that backend.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from omnimarket.handlers.emission_sink import ChainEmitter, ProtocolEmissionSink
from omnimarket.models.floor_17a import ModelPieceEventRule
from omnimarket.models.model_gate_store_join_j6 import (
    EnumJoinJ6Operation,
    EnumJoinJ6Outcome,
    ModelJoinJ6Request,
)
from omnimarket.models.model_gate_store_join_j6_run import (
    EnumJoinJ6RunOutcome,
    ModelJoinJ6RunCommand,
    ModelJoinJ6RunItem,
    ModelJoinJ6RunResult,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    piece_events,
)
from omnimarket.nodes.node_gate_store_effect.handlers.adapters_join_j6 import (
    InMemoryJoinJ6AuditSink,
    InMemoryJoinJ6Projection,
)
from omnimarket.nodes.node_gate_store_effect.handlers.handler_gate_store_join_j6 import (
    HandlerGateStoreJoinJ6,
)
from omnimarket.nodes.node_gate_store_effect.overlay_join_j6 import (
    ModelJoinJ6Overlay,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
)

_FUTURE_SLACK = timedelta(days=1)
_FENCE_CLOSED_CODE = "fence_closed_stop_unanswered"


@dataclass(frozen=True)
class JoinJ6Rig:
    """One run's join over a fresh gate store, with the sinks it writes so the run can read them back."""

    join: HandlerGateStoreJoinJ6
    audit: InMemoryJoinJ6AuditSink
    projection: InMemoryJoinJ6Projection
    release: Callable[[], None] = lambda: None


class HandlerGateStoreJoinJ6Run:
    def __init__(
        self,
        *,
        source: ProtocolLedgerRowSource,
        sink: ProtocolEmissionSink,
        upstream: ModelPieceEventRule,
        overlay: ModelJoinJ6Overlay,
        golden_rig: Callable[[], JoinJ6Rig],
        drill_rig: Callable[[], JoinJ6Rig],
        window_hours: int,
        to_piece: int,
        backend: str = "in_memory",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._source = source
        self._sink = sink
        self._rule = upstream
        self._overlay = overlay
        self._golden_rig = golden_rig
        self._drill_rig = drill_rig
        self._window = timedelta(hours=window_hours)
        self._to_piece = to_piece
        self._backend = backend
        self._clock = clock or (lambda: datetime.now(UTC))

    def handle(self, request: ModelJoinJ6RunCommand) -> ModelJoinJ6RunResult:
        now = self._clock()
        rule = self._rule
        read = self._source.read(
            ModelLedgerQuery(
                since=now - timedelta(days=rule.lookback_days),
                until=now + _FUTURE_SLACK,
                row_type=rule.golden.row_type,
                lane=rule.golden.lane,
                correlation_id=request.correlation_id,
            )
        )
        if read.error is not None:
            return ModelJoinJ6RunResult(
                read_ok=False, reason=f"ledger unread: {read.error}"
            )
        since = now - self._window
        golden = [
            e
            for e in piece_events(list(read.rows), (rule,))
            if e.phase == "success" and not e.drill and e.emitted_at >= since
        ]
        journal = self._sink.read(since)
        answered_ids = {e.correlation_id for e in journal if e.piece == self._to_piece}
        drill_since = now - timedelta(hours=self._overlay.drill_window_hours)
        drill_owed = not any(
            e.piece == self._to_piece
            and e.drill
            and e.phase == "failure"
            and e.emitted_at >= drill_since
            for e in journal
        )
        seen: set[str] = set()
        owed: list[str] = []
        answered = 0
        for event in sorted(golden, key=lambda e: e.emitted_at, reverse=True):
            cid = event.correlation_id
            if cid in seen:
                continue
            seen.add(cid)
            if cid in answered_ids:
                answered += 1
            else:
                owed.append(cid)
        if request.probe:
            return ModelJoinJ6RunResult(
                read_ok=True, owed=len(owed), answered=answered, drill_owed=drill_owed
            )
        items = [self._answer(cid) for cid in owed[: self._overlay.max_chains_per_run]]
        if drill_owed:
            items.append(self._fence_drill())
        return ModelJoinJ6RunResult(
            read_ok=True,
            owed=len(owed),
            answered=answered,
            drill_owed=drill_owed,
            items=tuple(items),
        )

    def _request(
        self,
        cid: str,
        operation: EnumJoinJ6Operation,
        piece: str,
        step: str,
        reservation_id: str | None = None,
    ) -> ModelJoinJ6Request:
        return ModelJoinJ6Request(
            correlation_id=cid,
            operation=operation,
            piece=piece,
            step_key=f"{step}-{cid}",
            reservation_id=reservation_id,
            now=self._clock().timestamp(),
        )

    def _answer(self, cid: str) -> ModelJoinJ6RunItem:
        ov = self._overlay
        chain = ChainEmitter(
            self._sink,
            node=ov.emission_node,
            piece=self._to_piece,
            clock=self._clock,
            correlation_id=cid,
            backend=self._backend,
        )
        chain.start()
        rig = self._golden_rig()
        try:
            stop = rig.join.handle(
                self._request(cid, EnumJoinJ6Operation.STOP, ov.stopped_piece, "stop")
            )
            if stop.outcome is not EnumJoinJ6Outcome.STOP_COMMITTED:
                return self._failed(
                    chain,
                    cid,
                    EnumJoinJ6RunOutcome.STOP_NOT_COMMITTED,
                    stop.failure_code or stop.outcome.value,
                    stop.detail,
                )
            durable = rig.audit.rows_for(ov.stopped_piece)
            projected = rig.projection.rows_for(ov.stopped_piece)
            if len(durable) != 1 or durable != projected:
                return self._failed(
                    chain,
                    cid,
                    EnumJoinJ6RunOutcome.ROWS_NOT_LANDED,
                    EnumJoinJ6RunOutcome.ROWS_NOT_LANDED.value,
                    f"{len(durable)} durable and {len(projected)} projected rows for the stopped piece",
                )
            refused = rig.join.handle(
                self._request(
                    cid, EnumJoinJ6Operation.SEND, ov.stopped_piece, "send", "res-a"
                )
            )
            if (
                refused.outcome is not EnumJoinJ6Outcome.SEND_REFUSED
                or refused.store_outcome != "refused_stop"
            ):
                return self._failed(
                    chain,
                    cid,
                    EnumJoinJ6RunOutcome.SEND_NOT_REFUSED,
                    EnumJoinJ6RunOutcome.SEND_NOT_REFUSED.value,
                    f"a send of the stopped piece read {refused.outcome.value} / {refused.store_outcome}",
                )
            other = rig.join.handle(
                self._request(
                    cid, EnumJoinJ6Operation.SEND, ov.other_piece, "send-other", "res-b"
                )
            )
            if other.outcome is not EnumJoinJ6Outcome.SEND_ADMITTED:
                return self._failed(
                    chain,
                    cid,
                    EnumJoinJ6RunOutcome.OTHER_PIECE_NOT_ADMITTED,
                    EnumJoinJ6RunOutcome.OTHER_PIECE_NOT_ADMITTED.value,
                    f"a send of another piece read {other.outcome.value}",
                )
            chain.success(
                {
                    "audit_rows": 1,
                    "projected_rows": 1,
                    "sends_refused": 1,
                    "sends_admitted": 1,
                }
            )
            return ModelJoinJ6RunItem(
                correlation_id=cid, outcome=EnumJoinJ6RunOutcome.LANDED
            )
        finally:
            rig.release()

    def _failed(
        self,
        chain: ChainEmitter,
        cid: str,
        outcome: EnumJoinJ6RunOutcome,
        code: str,
        detail: str,
    ) -> ModelJoinJ6RunItem:
        chain.failure(code, detail)
        return ModelJoinJ6RunItem(
            correlation_id=cid, outcome=outcome, failure_code=code, detail=detail
        )

    def _fence_drill(self) -> ModelJoinJ6RunItem:
        """A stop with no answer by its deadline: the stopped piece's send is refused, another piece's is admitted."""
        ov = self._overlay
        cid = f"drill-j6-{uuid4().hex}"
        rig = self._drill_rig()
        try:
            stop = rig.join.handle(
                self._request(cid, EnumJoinJ6Operation.STOP, ov.stopped_piece, "stop")
            )
            sent = rig.join.handle(
                self._request(
                    cid, EnumJoinJ6Operation.SEND, ov.stopped_piece, "send", "res-a"
                )
            )
            other = rig.join.handle(
                self._request(
                    cid, EnumJoinJ6Operation.SEND, ov.other_piece, "send-other", "res-b"
                )
            )
        finally:
            rig.release()
        closed = (
            stop.outcome is EnumJoinJ6Outcome.STOP_UNANSWERED
            and sent.outcome is EnumJoinJ6Outcome.SEND_REFUSED
            and sent.store_outcome is None
            and other.outcome is EnumJoinJ6Outcome.SEND_ADMITTED
        )
        if not closed:
            return ModelJoinJ6RunItem(
                correlation_id=cid,
                outcome=EnumJoinJ6RunOutcome.DRILL_NOT_HELD,
                drill=True,
                failure_code=EnumJoinJ6RunOutcome.DRILL_NOT_HELD.value,
                detail=(
                    f"stop {stop.outcome.value}; send of the stopped piece {sent.outcome.value}"
                    f" / {sent.store_outcome}; send of another piece {other.outcome.value}"
                ),
            )
        chain = ChainEmitter(
            self._sink,
            node=ov.emission_node,
            piece=self._to_piece,
            clock=self._clock,
            correlation_id=cid,
            backend=self._backend,
            drill=True,
        )
        chain.start()
        chain.failure(_FENCE_CLOSED_CODE, stop.detail)
        return ModelJoinJ6RunItem(
            correlation_id=cid,
            outcome=EnumJoinJ6RunOutcome.DRILL_RECORDED,
            drill=True,
            failure_code=_FENCE_CLOSED_CODE,
            detail=stop.detail,
        )


__all__ = ["HandlerGateStoreJoinJ6Run", "JoinJ6Rig"]
