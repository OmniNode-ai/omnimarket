# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed def-B handler: read the window from the lab database, replay it with
the markdown ledger's read rules, return the typed answer.

A database error is returned as ``error`` with every answer section empty and
no freshness, so a caller can never read a failed read as "no rows".
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from omnimarket.models.work_ledger_query import (
    EnumWorkLedgerParityStatus,
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerFreshness,
    ModelWorkLedgerParity,
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerQueryResult,
)
from omnimarket.nodes.contract_topics import contract_publish_topics
from omnimarket.nodes.node_work_ledger_query_effect.handlers import (
    work_ledger_query_replay as replay,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    PostgresWorkLedgerQueryReader,
    ProtocolWorkLedgerQueryReader,
    load_work_ledger_query_source,
)

_CONTRACT_PATH = Path(__file__).parent.parent / "contract.yaml"
# A daily receipt for day D is appended after D ends, so its rows are read
# past the window's end by this much.
_RECEIPT_LAG = timedelta(days=2)
# An unbounded window reports parity for its last days only, and never as exact.
_PARITY_DAYS_CAP = 31


class HandlerWorkLedgerQuery:
    def __init__(
        self,
        *,
        contract_path: Path | None = None,
        now: Callable[[], datetime] | None = None,
        reader: ProtocolWorkLedgerQueryReader | None = None,
    ) -> None:
        self._contract_path = contract_path or _CONTRACT_PATH
        self.terminal_event = contract_publish_topics(self._contract_path)[0]
        self._now = now if now is not None else lambda: datetime.now(UTC)
        self._reader = (
            reader
            if reader is not None
            else PostgresWorkLedgerQueryReader(
                load_work_ledger_query_source(self._contract_path)
            )
        )

    def handle(
        self, request: ModelWorkLedgerQueryRequest
    ) -> ModelWorkLedgerQueryResult:
        if not isinstance(request, ModelWorkLedgerQueryRequest):
            raise TypeError("work ledger query handler requires its contract payload")
        now = request.now or self._now()
        until = request.until or now
        base = ModelWorkLedgerQueryResult(
            correlation_id=request.correlation_id,
            query=request.query,
            window_since=request.since,
            window_until=until,
        )
        try:
            records = self._reader.read_rows(since=request.since, until=until)
            newest_ts, newest_projected = self._reader.freshness(until=until)
            first = request.since or (min((r.row_ts for r in records), default=until))
            days = replay.utc_days(first, until)
            capped = len(days) > _PARITY_DAYS_CAP
            days = days[-_PARITY_DAYS_CAP:]
            receipts = self._reader.read_parity_receipts(
                since=datetime.fromisoformat(days[0]).replace(tzinfo=UTC),
                until=until + _RECEIPT_LAG,
            )
        except Exception as exc:  # the reader already redacts connection details
            with contextlib.suppress(Exception):
                close = getattr(self._reader, "close", None)
                if close is not None:
                    close()
            return base.model_copy(update={"error": f"{type(exc).__name__}: {exc}"})
        parity = replay.window_parity(days, replay.parse_rows(receipts))
        if capped and parity.status is EnumWorkLedgerParityStatus.EXACT:
            parity = ModelWorkLedgerParity(
                status=EnumWorkLedgerParityStatus.UNMEASURED, days=parity.days
            )
        answer = self._answer(request, replay.parse_rows(records), now)
        return base.model_copy(
            update={
                **answer,
                "freshness": ModelWorkLedgerFreshness(
                    newest_row_ts=newest_ts,
                    newest_projected_at=newest_projected,
                    rows_read=len(records),
                ),
                "parity": parity,
            }
        )

    @staticmethod
    def _answer(
        request: ModelWorkLedgerQueryRequest,
        rows: list[replay.LedgerRow],
        now: datetime,
    ) -> dict[str, object]:
        kind = request.query
        if kind in {EnumWorkLedgerQueryKind.ROWS, EnumWorkLedgerQueryKind.RULINGS}:
            kinds = request.kinds or (
                replay.RULING_TYPES if kind is EnumWorkLedgerQueryKind.RULINGS else ()
            )
            matched = replay.select_rows(
                rows,
                lane=request.lane,
                pr=request.pr,
                ticket=request.ticket,
                kinds=kinds,
                row_ref=request.row_ref,
                term=request.term,
            )
            shown = matched[-request.limit :] if request.limit else matched
            return {"matched": len(matched), "rows": tuple(r.record for r in shown)}
        if kind is EnumWorkLedgerQueryKind.HOLDS:
            holds, counts = replay.holds_in_force(
                rows,
                now=now,
                pr=request.pr,
                repo=request.repo,
                surface=request.surface,
                to=request.to,
            )
            return {"matched": len(holds), "holds": tuple(holds), "hold_counts": counts}
        if kind is EnumWorkLedgerQueryKind.INBOX:
            lanes = request.lanes or ((request.lane,) if request.lane else ())
            boxes = tuple(replay.inbox(rows, lane) for lane in lanes)
            return {"matched": sum(len(b.entries) for b in boxes), "inbox": boxes}
        if kind is EnumWorkLedgerQueryKind.OPEN_CLAIMS:
            claims = replay.open_claims(
                rows, lane=request.lane, claims_since=request.claims_since
            )
            return {"matched": len(claims), "open_claims": tuple(claims)}
        newest = replay.newest_per_lane(rows, kinds=request.kinds)
        return {"matched": len(newest), "rows": tuple(r.record for r in newest)}


__all__ = ["HandlerWorkLedgerQuery"]
