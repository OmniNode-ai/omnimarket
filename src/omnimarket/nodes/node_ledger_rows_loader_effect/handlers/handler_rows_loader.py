# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The rows of a window of the work ledger, from the bus-fed reader or the counted file fallback.

Cutover plan step L7 (OMN-19609, OMN-23977): the shared loaders (the drain map rows load, the
landing controller's facts, queue-status) read the ledger file. The rows of a window are answered
here from ``node_work_ledger_reader_effect``'s rows read. An ok answer carries the node's lines,
stamped ``source=node``. A stale, unreadable or not-moved answer reads the file's rows while the
overlay allows it (until plan step L15), stamped ``source=file-fallback`` and COUNTED: the count of
file-fallback answers is the cutover measure, and it must reach zero before the file is retired.
When no source answered the result is ``unreadable`` with no lines, never an empty ok.

Every answer emits one event: ``answered`` (golden) for ok, ``refused`` (error) for unreadable.
Named break: a handler that returns an empty ok for a non-ok answer.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.models.model_work_ledger_reader import (
    EnumReaderEventKind,
    EnumReaderStatus,
)
from omnimarket.nodes.node_ledger_rows_loader_effect.models import (
    EnumRowsSource,
    EnumRowsStatus,
    ModelRowsLoaderEvent,
    ModelRowsLoaderOverlay,
    ModelRowsLoaderRequest,
    ModelRowsLoaderResult,
)
from omnimarket.nodes.node_ledger_rows_loader_effect.protocols import (
    ProtocolFileRows,
    ProtocolRowsLoaderEventSink,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


class HandlerRowsLoader:
    """``handle(ModelRowsLoaderRequest) -> ModelRowsLoaderResult``."""

    def __init__(
        self,
        overlay: ModelRowsLoaderOverlay,
        events: ProtocolRowsLoaderEventSink,
        file_rows: ProtocolFileRows | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        topics: Mapping[str, str] | None = None,
    ) -> None:
        self._overlay = overlay
        self._events = events
        self._file_rows = file_rows
        self._clock = clock
        self._topics = dict(load_topics() if topics is None else topics)

    def handle(self, request: ModelRowsLoaderRequest) -> ModelRowsLoaderResult:
        answer = request.node_answer
        prior = self._events.fallback_reads()
        if answer.status is EnumReaderStatus.OK:
            return self._finish(
                request,
                EnumRowsStatus.OK,
                EnumRowsSource.NODE,
                "node-answered",
                answer.lines,
                prior,
            )
        if not self._overlay.file_fallback_allowed:
            return self._unreadable(request, "file-fallback-retired", prior)
        if self._file_rows is None:
            return self._unreadable(request, "node-failed-and-no-file-source", prior)
        try:
            lines = self._file_rows.read()
        except OSError:
            return self._unreadable(request, "file-fallback-unreadable", prior)
        return self._finish(
            request,
            EnumRowsStatus.OK,
            EnumRowsSource.FILE_FALLBACK,
            f"node-{answer.status.value}",
            lines,
            prior + 1,
        )

    def _unreadable(
        self, request: ModelRowsLoaderRequest, reason: str, fallback_count: int
    ) -> ModelRowsLoaderResult:
        return self._finish(
            request,
            EnumRowsStatus.UNREADABLE,
            EnumRowsSource.NONE,
            reason,
            (),
            fallback_count,
        )

    def _finish(
        self,
        request: ModelRowsLoaderRequest,
        status: EnumRowsStatus,
        source: EnumRowsSource,
        reason: str,
        lines: tuple[str, ...],
        fallback_count: int,
    ) -> ModelRowsLoaderResult:
        golden = status is EnumRowsStatus.OK
        event = ModelRowsLoaderEvent(
            kind=EnumReaderEventKind.ANSWERED
            if golden
            else EnumReaderEventKind.REFUSED,
            topic=self._topics[
                "rows_loader_answered" if golden else "rows_loader_refused"
            ],
            reader=request.reader,
            source=source,
            status=status,
            reason=reason,
            at=self._clock().astimezone(UTC),
            row_count=len(lines),
            fallback_count=fallback_count,
        )
        self._events.emit(event)
        return ModelRowsLoaderResult(
            reader=request.reader,
            status=status,
            source=source,
            reason=reason,
            lines=lines,
            row_count=len(lines),
            fallback_count=fallback_count,
            event=event,
        )


__all__ = ["HandlerRowsLoader"]
