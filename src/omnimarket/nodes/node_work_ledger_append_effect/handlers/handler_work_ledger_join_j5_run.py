# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""J5 run (OMN-23086): answer each piece 5 golden reading with a ledger append under the store's identity.

Canonical shape: ``handle(ModelJoinJ5RunCommand) -> ModelJoinJ5RunResult``. The join handler (``HandlerWorkLedgerJoinJ5``)
judges one append; nothing called it, so the floor's J5 never saw a piece 6 chain beside a piece 5 success. This is the
caller. Piece 5's golden chains are the floor's own: the secret reading rows of the ledger, read through the upstream rule
the floor reads them with, under the correlation id the floor gives each row. For each such chain the journal does not
show piece 6 answering, this run asks the join handler to append one row under the identity the store resolves for the
overlay's reference, read the row back from the projection, and write piece 6's start and success (or typed failure) to
the journal under that chain's id. A chain piece 6 already answered is not answered again, so a rerun appends nothing.

An unreadable ledger is never an empty answer: nothing is owed or written, and the result says so. A probe reads what is
owed and writes nothing.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from omnimarket.handlers.emission_sink import ProtocolEmissionSink
from omnimarket.models.floor_17a import ModelPieceEventRule
from omnimarket.models.work_ledger_append import (
    ModelJoinJ5Request,
    ModelJoinJ5RunCommand,
    ModelJoinJ5RunItem,
    ModelJoinJ5RunResult,
    ModelWorkLedgerAppendRequest,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    piece_events,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
)
from omnimarket.nodes.node_work_ledger_append_effect.handlers.handler_work_ledger_join_j5 import (
    HandlerWorkLedgerJoinJ5,
)

_FUTURE_SLACK = timedelta(days=1)


class HandlerWorkLedgerJoinJ5Run:
    def __init__(
        self,
        *,
        join: HandlerWorkLedgerJoinJ5,
        source: ProtocolLedgerRowSource,
        sink: ProtocolEmissionSink,
        upstream: ModelPieceEventRule,
        identity_ref: str,
        ledger_id: str,
        row_lane: str,
        requesting_lane: str,
        row_text: str,
        host: str,
        window_hours: int,
        max_appends: int,
        to_piece: int,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._join = join
        self._source = source
        self._sink = sink
        self._rule = upstream
        self._identity_ref = identity_ref
        self._ledger_id = ledger_id
        self._row_lane = row_lane
        self._requesting_lane = requesting_lane
        self._row_text = row_text
        self._host = host
        self._window = timedelta(hours=window_hours)
        self._max = max_appends
        self._to_piece = to_piece
        self._clock = clock or (lambda: datetime.now(UTC))

    def handle(self, request: ModelJoinJ5RunCommand) -> ModelJoinJ5RunResult:
        now = self._clock()
        rule = self._rule
        read = self._source.read(
            ModelLedgerQuery(
                since=now - timedelta(days=rule.lookback_days),
                until=now + _FUTURE_SLACK,
                row_type=rule.golden.row_type,
                lane=rule.golden.lane,
            )
        )
        if read.error is not None:
            return ModelJoinJ5RunResult(
                read_ok=False, reason=f"ledger unread: {read.error}"
            )
        since = now - self._window
        golden = [
            e
            for e in piece_events(list(read.rows), (rule,))
            if e.phase == "success" and not e.drill and e.emitted_at >= since
        ]
        answered_ids = {
            e.correlation_id
            for e in self._sink.read(since)
            if e.piece == self._to_piece
        }
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
            return ModelJoinJ5RunResult(read_ok=True, owed=len(owed), answered=answered)
        items = tuple(self._answer(cid, now) for cid in owed[: self._max])
        return ModelJoinJ5RunResult(
            read_ok=True, owed=len(owed), answered=answered, items=items
        )

    def _answer(self, cid: str, now: datetime) -> ModelJoinJ5RunItem:
        request_id = uuid4()
        row = (
            f"{now.strftime('%Y-%m-%dT%H:%M:%SZ')} | STATUS | lane={self._row_lane}"
            f" | src=join-j5 | join=J5 | cid={cid} | req={request_id} | {self._row_text}"
        )
        result = self._join.handle(
            ModelJoinJ5Request(
                correlation_id=cid,
                identity_ref=self._identity_ref,
                append=ModelWorkLedgerAppendRequest(
                    request_id=request_id,
                    ledger_id=self._ledger_id,
                    rows=row,
                    requested_by_lane=self._requesting_lane,
                    requesting_host=self._host,
                    requested_at=now,
                ),
            )
        )
        return ModelJoinJ5RunItem(
            correlation_id=cid,
            outcome=result.outcome,
            failure_code=result.failure_code,
            ledger_seq=result.ledger_seq,
        )


__all__ = ["HandlerWorkLedgerJoinJ5Run"]
