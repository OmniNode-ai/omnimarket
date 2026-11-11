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
    DEFAULT_POINT_PAGE_SIZE,
    POINT_QUERY_KINDS,
    EnumWorkLedgerParityStatus,
    EnumWorkLedgerQueryKind,
    ModelWorkLedgerFreshness,
    ModelWorkLedgerParity,
    ModelWorkLedgerQueryRequest,
    ModelWorkLedgerQueryResult,
    ModelWorkLedgerRowRecord,
    ModelWorkLedgerStateQuery,
)
from omnimarket.nodes.contract_topics import contract_publish_topics
from omnimarket.nodes.node_work_ledger_query_effect.handlers import (
    work_ledger_query_replay as replay,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_answer_bound import (
    bound_answer,
    load_work_ledger_answer_bound,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_history import (
    history_answer,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    PostgresWorkLedgerQueryReader,
    ProtocolWorkLedgerFilteredRowReader,
    ProtocolWorkLedgerOldestRowReader,
    ProtocolWorkLedgerQueryReader,
    ProtocolWorkLedgerStateReader,
    WorkLedgerReadError,
    load_work_ledger_query_source,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_rows_page import (
    rows_page,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_state_answer import (
    decode_cursor,
    encode_cursor,
    hold_lines,
    inbox_answer,
    load_hold_stale_after,
    parse_claims_since,
    partition_stale_holds,
    state_answer,
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
        self._answer_bound = load_work_ledger_answer_bound(self._contract_path)
        self._hold_stale_after = load_hold_stale_after(self._contract_path)
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
        """Answer the request, or refuse it when its answer would not fit the bound."""
        if not isinstance(request, ModelWorkLedgerQueryRequest):
            raise TypeError("work ledger query handler requires its contract payload")
        return bound_answer(request, self._answer_request(request), self._answer_bound)

    def _answer_request(
        self, request: ModelWorkLedgerQueryRequest
    ) -> ModelWorkLedgerQueryResult:
        now = request.now or self._now()
        until = request.until or now
        base = ModelWorkLedgerQueryResult(
            correlation_id=request.correlation_id,
            query=request.query,
            window_since=request.since,
            window_until=until,
        )
        if request.history is not None:
            try:
                assert request.history is not None
                records = self._reader.read_history(
                    ledger_id=request.history.ledger_id,
                    first_seq=request.history.first_seq,
                    last_seq=request.history.last_seq,
                    until=until,
                )
                return base.model_copy(update=history_answer(request, records))
            except WorkLedgerReadError as exc:
                return self._fail(base, str(exc))
            except Exception as exc:
                return self._fail(
                    base, f"{type(exc).__name__}: history acquisition failed"
                )
        if request.query in POINT_QUERY_KINDS:
            if isinstance(self._reader, ProtocolWorkLedgerStateReader):
                return self._point_query(request, base, now)
            if request.query is EnumWorkLedgerQueryKind.OPEN_RULINGS:
                return self._fail(
                    base, "open-rulings needs a reader backed by work_ledger_state"
                )
        try:
            records = self._read_rows(request, until)
            newest_ts, newest_projected = self._reader.freshness(until=until)
            first = request.since
            if first is None:
                oldest = self._reader
                first = (
                    oldest.oldest_row_ts(until=until)
                    if isinstance(oldest, ProtocolWorkLedgerOldestRowReader)
                    and self._narrowed_without_start(request)
                    else min((r.row_ts for r in records), default=until)
                )
            first = first or until
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
            return self._fail(base, self._message(exc))
        parity = replay.window_parity(days, replay.parse_rows(receipts))
        if capped and parity.status is EnumWorkLedgerParityStatus.EXACT:
            parity = ModelWorkLedgerParity(
                status=EnumWorkLedgerParityStatus.UNMEASURED, days=parity.days
            )
        parsed = replay.parse_rows(records)
        if request.principal is not None:
            parsed = [r for r in parsed if r.token("principal") == request.principal]
        if (
            request.query is EnumWorkLedgerQueryKind.ROWS
            and request.page_size is not None
        ):
            try:
                total, page, next_cursor = rows_page(
                    parsed,
                    lanes=request.lanes or ((request.lane,) if request.lane else ()),
                    page_size=request.page_size,
                    cursor=request.cursor,
                    kinds=request.kinds,
                    since=request.since,
                    ticket=request.ticket,
                    pr=request.pr,
                    row_ref=request.row_ref,
                    term=request.term,
                )
            except WorkLedgerReadError as exc:
                return self._fail(base, str(exc), page_size=request.page_size)
            answer = {
                "matched": total,
                "rows": tuple(page),
                "lines": tuple(r.text for r in page),
                "page_size": request.page_size,
                "next_cursor": next_cursor,
            }
        else:
            answer = self._answer(request, parsed, now, self._hold_stale_after)
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

    def _read_rows(
        self, request: ModelWorkLedgerQueryRequest, until: datetime
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        """The window's rows, narrowed in the store when the read can be (OMN-19609, OMN-23615).

        Only the rows and rulings answers are narrowed. With a start the store keeps the asked row types and
        lane; with no start it also keeps the asked term, and the parity window is then taken from the
        ledger's oldest row (``oldest_row_ts``) so it does not depend on the rows read. Every other answer
        reads the window whole. The narrowing is a superset of what select_rows keeps, so the answer is the same.
        """
        reader = self._reader
        no_start = self._narrowed_without_start(request)
        if (
            (request.since is not None or no_start)
            and request.query
            in {EnumWorkLedgerQueryKind.ROWS, EnumWorkLedgerQueryKind.RULINGS}
            and isinstance(reader, ProtocolWorkLedgerFilteredRowReader)
        ):
            kinds = self._narrow_kinds(request)
            term = request.term if no_start and request.term else None
            if kinds or request.lane or term:
                return reader.read_rows_matching(
                    since=request.since,
                    until=until,
                    kinds=tuple(k.upper() for k in kinds),
                    contains=request.lane or None,
                    **({"term": term} if term else {}),
                )
        return reader.read_rows(since=request.since, until=until)

    @staticmethod
    def _narrow_kinds(request: ModelWorkLedgerQueryRequest) -> tuple[str, ...]:
        return tuple(request.kinds) or (
            tuple(replay.RULING_TYPES)
            if request.query is EnumWorkLedgerQueryKind.RULINGS
            else ()
        )

    def _narrowed_without_start(self, request: ModelWorkLedgerQueryRequest) -> bool:
        """A rows read with no start that names a kind, a lane or an ASCII term is narrowed in the store (OMN-23615).

        The parity window of such a read starts at the ledger's oldest row, which the store names itself
        (``oldest_row_ts``), so the narrowed rows cannot move it; a store that cannot is read whole.
        """
        return (
            request.since is None
            and request.query
            in {EnumWorkLedgerQueryKind.ROWS, EnumWorkLedgerQueryKind.RULINGS}
            and isinstance(self._reader, ProtocolWorkLedgerFilteredRowReader)
            and isinstance(self._reader, ProtocolWorkLedgerOldestRowReader)
            and bool(
                self._narrow_kinds(request)
                or request.lane
                or (request.term and request.term.isascii())
            )
        )

    @staticmethod
    def _message(exc: Exception) -> str:
        if isinstance(exc, WorkLedgerReadError):
            return str(exc)
        return f"{type(exc).__name__}: {exc}"

    @staticmethod
    def _fail(
        base: ModelWorkLedgerQueryResult,
        message: str,
        *,
        page_size: int | None = None,
    ) -> ModelWorkLedgerQueryResult:
        """An error answer: no section is an answer, so none is filled."""
        return base.model_copy(
            update={"error": message, "status": "error", "page_size": page_size}
        )

    def _point_query(
        self,
        request: ModelWorkLedgerQueryRequest,
        base: ModelWorkLedgerQueryResult,
        now: datetime,
    ) -> ModelWorkLedgerQueryResult:
        """holds, inbox, open-claims and open-rulings from ``work_ledger_state``:
        one keyset page of the open set, with its size and the table's watermark."""
        page_size = request.page_size or DEFAULT_POINT_PAGE_SIZE
        reader = self._reader
        assert isinstance(reader, ProtocolWorkLedgerStateReader)
        try:
            query = ModelWorkLedgerStateQuery(
                point=request.query,
                now=now,
                limit=page_size,
                after=decode_cursor(request.cursor, request.query)
                if request.cursor
                else None,
                lane=request.lane,
                lanes=request.lanes or ((request.lane,) if request.lane else ()),
                ticket=request.ticket,
                repo=request.repo,
                pr=request.pr,
                surface=request.surface,
                to=request.to,
                claims_since=parse_claims_since(request.claims_since)
                if request.claims_since
                else None,
            )
            page = reader.read_state_page(query=query)
            answer = state_answer(request, page, now, self._hold_stale_after)
        except Exception as exc:  # the reader already redacts connection details
            with contextlib.suppress(Exception):
                close = getattr(reader, "close", None)
                if close is not None:
                    close()
            return self._fail(base, self._message(exc), page_size=page_size)
        return base.model_copy(
            update={
                **answer,
                "page_size": page_size,
                "watermark": page.watermark,
                "next_cursor": encode_cursor(request.query, page.entities[-1])
                if page.has_more and page.entities
                else None,
            }
        )

    @staticmethod
    def _answer(
        request: ModelWorkLedgerQueryRequest,
        rows: list[replay.LedgerRow],
        now: datetime,
        stale_after: timedelta,
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
            live, stale = partition_stale_holds(holds, now, stale_after)
            return {
                "matched": len(live),
                "holds": tuple(live),
                "hold_counts": counts,
                "lines": hold_lines(live, stale, counts, now, stale_after),
            }
        if kind is EnumWorkLedgerQueryKind.INBOX:
            lanes = request.lanes or ((request.lane,) if request.lane else ())
            return inbox_answer(rows, lanes, now, stale_after)
        if kind is EnumWorkLedgerQueryKind.OPEN_CLAIMS:
            claims = replay.open_claims(
                rows, lane=request.lane, claims_since=request.claims_since
            )
            return {"matched": len(claims), "open_claims": tuple(claims)}
        newest = replay.newest_per_lane(rows, kinds=request.kinds)
        shown = newest[-request.limit :] if request.limit else newest
        return {"matched": len(newest), "rows": tuple(r.record for r in shown)}


__all__ = ["HandlerWorkLedgerQuery"]
