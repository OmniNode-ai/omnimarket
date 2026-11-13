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
from typing import NamedTuple

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
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_latest_answer import (
    encode_latest_cursor,
    latest_query_for,
    load_latest_default_page_size,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    PostgresWorkLedgerQueryReader,
    ProtocolWorkLedgerFilteredRowReader,
    ProtocolWorkLedgerLatestReader,
    ProtocolWorkLedgerOldestRowReader,
    ProtocolWorkLedgerQueryReader,
    ProtocolWorkLedgerStateReader,
    WorkLedgerReadError,
    load_work_ledger_query_source,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_rows_bound import (
    bounded_since,
    load_rows_window,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_rows_page import (
    RowsCursor,
    decode_rows_cursor,
    encode_rows_cursor,
    page_of,
    select_ordered,
)
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_rows_window_cache import (
    RowsWindowCache,
    WindowSnapshot,
    load_rows_window_cache_settings,
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


class _WindowRead(NamedTuple):
    """One read of a window from the store, before any page is cut from it."""

    parsed: list[replay.LedgerRow]
    rows_read: int
    newest_row_ts: datetime | None
    newest_projected_at: datetime | None
    parity: ModelWorkLedgerParity


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
        rows_window_cache: RowsWindowCache | None = None,
    ) -> None:
        self._contract_path = contract_path or _CONTRACT_PATH
        self.terminal_event = contract_publish_topics(self._contract_path)[0]
        self._answer_bound = load_work_ledger_answer_bound(self._contract_path)
        self._hold_stale_after = load_hold_stale_after(self._contract_path)
        self._rows_window = load_rows_window(self._contract_path)
        self._latest_page_size = load_latest_default_page_size(self._contract_path)
        self._held_windows = (
            rows_window_cache
            if rows_window_cache is not None
            else RowsWindowCache(load_rows_window_cache_settings(self._contract_path))
        )
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
        page_request = (
            request.query is EnumWorkLedgerQueryKind.ROWS
            and request.page_size is not None
        )
        after: RowsCursor | None = None
        cursor_error: WorkLedgerReadError | None = None
        if page_request and request.cursor:
            try:
                after = decode_rows_cursor(request.cursor)
            except WorkLedgerReadError as exc:
                cursor_error = exc
        # A later page reads to the window end its cursor names, so the window does not move with
        # the clock between pages and the held copy of it can be found (OMN-24031).
        until = request.until or (after.until if after else None) or now
        bounded_start = bounded_since(request, until, self._rows_window)
        effective_since = bounded_start if bounded_start is not None else request.since
        base = ModelWorkLedgerQueryResult(
            correlation_id=request.correlation_id,
            query=request.query,
            window_since=effective_since,
            window_until=until,
            bounded=bounded_start is not None,
            bounded_window_days=(
                self._rows_window.days if bounded_start is not None else None
            ),
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
        if request.query is EnumWorkLedgerQueryKind.LATEST_STATE:
            return self._latest_query(request, base)
        if request.query in POINT_QUERY_KINDS:
            if isinstance(self._reader, ProtocolWorkLedgerStateReader):
                return self._point_query(request, base, now)
            if request.query is EnumWorkLedgerQueryKind.OPEN_RULINGS:
                return self._fail(
                    base, "open-rulings needs a reader backed by work_ledger_state"
                )
        if cursor_error is not None:
            return self._fail(base, str(cursor_error), page_size=request.page_size)
        key = self._held_key(request, until, effective_since) if page_request else None
        held = (
            self._held_windows.get(key)
            if key is not None and after is not None
            else None
        )
        try:
            read = (
                None
                if held is not None
                else self._read_window(request, until, effective_since)
            )
        except Exception as exc:  # the reader already redacts connection details
            with contextlib.suppress(Exception):
                close = getattr(self._reader, "close", None)
                if close is not None:
                    close()
            return self._fail(base, self._message(exc))
        freshness: _WindowRead | WindowSnapshot
        if page_request:
            assert request.page_size is not None
            window = held
            if window is None:
                assert read is not None
                try:
                    window = WindowSnapshot(
                        ordered=select_ordered(
                            read.parsed,
                            lanes=request.lanes
                            or ((request.lane,) if request.lane else ()),
                            kinds=request.kinds,
                            since=effective_since,
                            ticket=request.ticket,
                            pr=request.pr,
                            row_ref=request.row_ref,
                            term=request.term,
                        ),
                        rows_read=read.rows_read,
                        newest_row_ts=read.newest_row_ts,
                        newest_projected_at=read.newest_projected_at,
                        parity=read.parity,
                    )
                except WorkLedgerReadError as exc:
                    return self._fail(base, str(exc), page_size=request.page_size)
            total, page, more = page_of(
                window.ordered, page_size=request.page_size, after=after
            )
            if held is None and key is not None and more:
                self._held_windows.put(key, window)
            answer = {
                "matched": total,
                "rows": () if request.lines_only else tuple(page),
                "lines": tuple(r.text for r in page),
                "page_size": request.page_size,
                "next_cursor": encode_rows_cursor(page[-1], until)
                if more and page
                else None,
            }
            freshness = window
        else:
            assert read is not None
            answer = self._answer(request, read.parsed, now, self._hold_stale_after)
            freshness = read
        return base.model_copy(
            update={
                **answer,
                "freshness": ModelWorkLedgerFreshness(
                    newest_row_ts=freshness.newest_row_ts,
                    newest_projected_at=freshness.newest_projected_at,
                    rows_read=freshness.rows_read,
                ),
                "parity": freshness.parity,
            }
        )

    def _held_key(
        self,
        request: ModelWorkLedgerQueryRequest,
        until: datetime,
        since: datetime | None,
    ) -> tuple[object, ...]:
        """What makes two page requests one window: everything that decides the rows read and the
        selection cut from them, and nothing that decides the page (cursor, page_size, lines_only)."""
        return (
            since,
            until,
            request.lane,
            tuple(request.lanes),
            tuple(request.kinds),
            request.ticket,
            request.pr,
            request.row_ref,
            request.term,
            request.principal,
        )

    def _read_window(
        self,
        request: ModelWorkLedgerQueryRequest,
        until: datetime,
        effective_since: datetime | None,
    ) -> _WindowRead:
        """One read of the window from the store: rows, freshness and the window's parity."""
        records = self._read_rows(request, until, effective_since)
        newest_ts, newest_projected = self._reader.freshness(until=until)
        first = effective_since
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
        parity = replay.window_parity(days, replay.parse_rows(receipts))
        if capped and parity.status is EnumWorkLedgerParityStatus.EXACT:
            parity = ModelWorkLedgerParity(
                status=EnumWorkLedgerParityStatus.UNMEASURED, days=parity.days
            )
        parsed = replay.parse_rows(records)
        if request.principal is not None:
            parsed = [r for r in parsed if r.token("principal") == request.principal]
        return _WindowRead(parsed, len(records), newest_ts, newest_projected, parity)

    def _read_rows(
        self,
        request: ModelWorkLedgerQueryRequest,
        until: datetime,
        since: datetime | None,
    ) -> tuple[ModelWorkLedgerRowRecord, ...]:
        """The window's rows, narrowed in the store when the read can be (OMN-19609, OMN-23615).

        ``since`` is the effective start: the request's ``since`` or the bounded window's start
        (OMN-23541), so a bounded rows read narrows in the store too. Only the rows and rulings
        answers are narrowed. With a start the store keeps the asked row types and lane; with no
        start it also keeps the asked term, and the parity window is then taken from the ledger's
        oldest row (``oldest_row_ts``) so it does not depend on the rows read. Every other answer
        reads the window whole. The narrowing is a superset of what select_rows keeps, so the
        answer is the same.
        """
        reader = self._reader
        no_start = self._narrowed_without_start(request)
        if (
            (since is not None or no_start)
            and request.query
            in {EnumWorkLedgerQueryKind.ROWS, EnumWorkLedgerQueryKind.RULINGS}
            and isinstance(reader, ProtocolWorkLedgerFilteredRowReader)
        ):
            kinds = self._narrow_kinds(request)
            term = request.term if no_start and request.term else None
            if kinds or request.lane or term:
                return reader.read_rows_matching(
                    since=since,
                    until=until,
                    kinds=tuple(k.upper() for k in kinds),
                    contains=request.lane or None,
                    **({"term": term} if term else {}),
                )
        return reader.read_rows(since=since, until=until)

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

    def _latest_query(
        self,
        request: ModelWorkLedgerQueryRequest,
        base: ModelWorkLedgerQueryResult,
    ) -> ModelWorkLedgerQueryResult:
        """latest-state (OMN-23606): one keyset page of the newest row per lane or ticket and row type,
        with its size and the table's watermark. A reader with no latest table answers an error, never an empty page."""
        page_size = request.page_size or self._latest_page_size
        reader = self._reader
        if not isinstance(reader, ProtocolWorkLedgerLatestReader):
            return self._fail(
                base,
                "latest-state needs a reader backed by work_ledger_latest",
                page_size=page_size,
            )
        try:
            page = reader.read_latest_page(
                query=latest_query_for(request, limit=page_size)
            )
        except Exception as exc:  # the reader already redacts connection details
            with contextlib.suppress(Exception):
                close = getattr(reader, "close", None)
                if close is not None:
                    close()
            return self._fail(base, self._message(exc), page_size=page_size)
        return base.model_copy(
            update={
                "latest": page.entries,
                "matched": len(page.entries),
                "page_size": page_size,
                "watermark": page.watermark,
                "next_cursor": encode_latest_cursor(page.entries[-1])
                if page.has_more and page.entries
                else None,
            }
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
