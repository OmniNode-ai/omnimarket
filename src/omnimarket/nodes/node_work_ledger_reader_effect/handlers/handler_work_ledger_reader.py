# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A ledger reader moved off the file answers from the bus-fed query node, or not at all.

Operator ruling 2026-11-05 (the ledger on the event bus, no file ledger ever again) and the
orchestrator's piece 6 deliverable of 2026-11-06T13:53:20Z: every reader answers from the
bus-fed query node with its parity and freshness read, and a file read is refused once a
reader has moved. The overlay's ``moved_readers`` is the move: a reader named there is
answered from ``node_work_ledger_query_effect`` through the injected read port and never from
the file. A failed read is ``unreadable``, never an empty answer and never a file fallback; an
answer whose watermark is older than ``max_watermark_age_seconds`` is ``stale``. A reader
not named there gets ``not-moved`` and keeps its own read, which is the per-reader rollback
of the cutover plan (section 8, L5 to L8).

Every read emits one event: ``answered`` (the golden event) when the node answered within the
bound, ``refused`` (the error event) otherwise. Named break: a read route that answers with an
old watermark (the projection stopped while the node still answers) reads ``stale``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    EnumReaderEventKind,
    EnumReaderParity,
    EnumReaderQuery,
    EnumReaderStatus,
    ModelPointAnswer,
    ModelPointQuery,
    ModelReaderEvent,
    ModelReaderOverlay,
    ModelReaderRequest,
    ModelReaderResult,
)
from omnimarket.nodes.node_work_ledger_reader_effect.protocols import (
    ProtocolLedgerPointRead,
    ProtocolReaderEventSink,
)

_PARITY = {p.value: p for p in EnumReaderParity}
CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


# The query node's inbox summary line, one per page; a paged inbox sums them.
_INBOX_SUMMARY = re.compile(
    r"^inbox (?P<lane>\S+): (?P<open>\d+) open, (?P<action>\d+) need action "
    r"\(HOLD:(?P<hold>\d+) ask:(?P<ask>\d+)\), (?P<fyi>\d+) FYI, newest=(?P<newest>\S+)$"
)


def merge_pages(
    query: EnumReaderQuery, pages: list[ModelPointAnswer]
) -> ModelPointAnswer:
    """One answer from the pages of one open set: the oldest watermark, every line, and for an
    inbox one summary line summed over the pages' summary lines."""
    if len(pages) == 1:
        return pages[0]
    watermarks = [p.watermark for p in pages if p.watermark is not None]
    lines = [line for p in pages for line in p.lines]
    if query is EnumReaderQuery.INBOX:
        heads = [m for line in lines if (m := _INBOX_SUMMARY.match(line))]
        if len(heads) != len(pages):
            return ModelPointAnswer(error="inbox-page-without-summary-line")
        total = {
            k: sum(int(m[k]) for m in heads)
            for k in ("open", "action", "hold", "ask", "fyi")
        }
        dated = [m["newest"] for m in heads if m["newest"] != "none"]
        summary = (
            f"inbox {heads[0]['lane']}: {total['open']} open, {total['action']} need action "
            f"(HOLD:{total['hold']} ask:{total['ask']}), {total['fyi']} FYI, "
            f"newest={max(dated) if dated else 'none'}"
        )
        lines = [summary, *(line for line in lines if not _INBOX_SUMMARY.match(line))]
    if query is EnumReaderQuery.ROWS:
        # A rows window: the lines are every page's lines in page order, and the query node
        # reports the window total on every page, so the matched count is the first page's,
        # never summed over the pages.
        return ModelPointAnswer(
            lines=tuple(lines),
            matched=pages[0].matched,
            watermark=min(watermarks) if len(watermarks) == len(pages) else None,
        )
    return ModelPointAnswer(
        lines=tuple(lines),
        matched=sum(p.matched for p in pages),
        watermark=min(watermarks) if len(watermarks) == len(pages) else None,
    )


def _token(text: str) -> str:
    """A reason cell: lower-case words joined by hyphens, bounded."""
    out = "".join(c if c.isalnum() else "-" for c in text.lower())
    return "-".join(part for part in out.split("-") if part)[:80] or "unknown"


class HandlerWorkLedgerReader:
    """``handle(ModelReaderRequest) -> ModelReaderResult``."""

    def __init__(
        self,
        overlay: ModelReaderOverlay,
        point_read: ProtocolLedgerPointRead,
        events: ProtocolReaderEventSink,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        topics: Mapping[str, str] | None = None,
    ) -> None:
        self._topics = dict(load_topics() if topics is None else topics)
        self._overlay = overlay
        self._read = point_read
        self._events = events
        self._clock = clock

    def handle(self, request: ModelReaderRequest) -> ModelReaderResult:
        bound = self._overlay.max_watermark_age_seconds
        if request.reader not in self._overlay.moved_readers:
            # Not moved: the caller keeps its own read. No event: nothing was read here.
            return ModelReaderResult(
                reader=request.reader,
                query=request.query,
                status=EnumReaderStatus.NOT_MOVED,
                reason="reader-not-in-moved-readers",
                source="none",
                max_watermark_age_s=bound,
            )
        if request.query is EnumReaderQuery.INBOX and not request.lane:
            raise ValueError("an inbox read names its lane")
        now = self._clock().astimezone(UTC)
        answer = self._read_open_set(request, now)
        if answer.error is not None:
            return self._finish(
                request,
                EnumReaderStatus.UNREADABLE,
                f"node-read-failed-{_token(answer.error)}",
                answer=None,
                age=None,
                parity=EnumReaderParity.UNREAD,
            )
        if answer.watermark is None:
            return self._finish(
                request,
                EnumReaderStatus.UNREADABLE,
                "node-answer-without-watermark",
                answer=None,
                age=None,
                parity=EnumReaderParity.UNREAD,
            )
        age = max(0, round((now - answer.watermark).total_seconds()))
        parity = self._parity(now) if request.read_parity else EnumReaderParity.UNREAD
        if age > bound:
            return self._finish(
                request,
                EnumReaderStatus.STALE,
                "watermark-older-than-bound",
                answer=answer,
                age=age,
                parity=parity,
            )
        return self._finish(
            request,
            EnumReaderStatus.OK,
            "node-answered-fresh",
            answer=answer,
            age=age,
            parity=parity,
        )

    def _read_open_set(
        self, request: ModelReaderRequest, now: datetime
    ) -> ModelPointAnswer:
        """Every page of the open set, following next_cursor up to max_pages; a set that does
        not end within max_pages is an error, never a cut-short answer. A rows read names its
        window: since is the injected clock's now minus window_hours, read once at the start
        of handle and used for every page."""
        pages: list[ModelPointAnswer] = []
        cursor: str | None = None
        for _ in range(self._overlay.max_pages):
            page = self._read.read(
                ModelPointQuery(
                    query=request.query.value,
                    lanes=(request.lane,)
                    if request.query is EnumReaderQuery.INBOX and request.lane
                    else (),
                    since=(
                        now - timedelta(hours=request.window_hours)
                        if request.query is EnumReaderQuery.ROWS
                        and request.window_hours is not None
                        else None
                    ),
                    page_size=self._overlay.page_size,
                    cursor=cursor,
                )
            )
            if page.error is not None:
                return page
            pages.append(page)
            cursor = page.next_cursor
            if cursor is None:
                return merge_pages(request.query, pages)
        return ModelPointAnswer(
            error=f"open-set-longer-than-{self._overlay.max_pages}-pages"
        )

    def _parity(self, now: datetime) -> EnumReaderParity:
        """The window parity the query node computes from the daily parity receipts."""
        settings = self._overlay.parity
        answer = self._read.read(
            ModelPointQuery(
                query="rows",
                lane=settings.receipt_lane,
                since=now - timedelta(hours=settings.window_hours),
                limit=1,
            )
        )
        if answer.error is not None or answer.parity is None:
            return EnumReaderParity.UNREAD
        return _PARITY.get(answer.parity, EnumReaderParity.UNREAD)

    def _finish(
        self,
        request: ModelReaderRequest,
        status: EnumReaderStatus,
        reason: str,
        *,
        answer: ModelPointAnswer | None,
        age: int | None,
        parity: EnumReaderParity,
    ) -> ModelReaderResult:
        golden = status is EnumReaderStatus.OK
        event = ModelReaderEvent(
            kind=EnumReaderEventKind.ANSWERED
            if golden
            else EnumReaderEventKind.REFUSED,
            topic=self._topics["reader_answered" if golden else "reader_refused"],
            reader=request.reader,
            query=request.query,
            status=status,
            reason=reason,
            at=self._clock().astimezone(UTC),
            watermark_age_s=age,
            parity=parity,
            matched=answer.matched if answer else 0,
        )
        self._events.emit(event)
        return ModelReaderResult(
            reader=request.reader,
            query=request.query,
            status=status,
            reason=reason,
            source="node" if answer is not None else "none",
            lines=answer.lines if answer else (),
            matched=answer.matched if answer else 0,
            watermark=answer.watermark if answer else None,
            watermark_age_s=age,
            max_watermark_age_s=self._overlay.max_watermark_age_seconds,
            parity=parity,
            event=event,
        )


__all__ = ["HandlerWorkLedgerReader"]
