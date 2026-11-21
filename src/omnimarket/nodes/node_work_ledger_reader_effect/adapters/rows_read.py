# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The rows of a window of the work ledger, read through the moved ledger reader and never from the file.

A consumer that used to open the ledger file for the rows of a window asks this function instead. It
builds the reader handler from the host's reader overlay, asks for a ``rows`` read of the named reader
and returns the node's lines. Anything but an ``ok`` answer (the reader is not named in the overlay's
``moved_readers``, the node did not answer, the answer is older than the freshness bound) is
:class:`LedgerRowsUnreadableError`: a failed read is never an empty list and never a file read in its place.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_work_ledger_reader_effect.adapters.point_read_adapters import (
    InMemoryEventSink,
    JournalEventSink,
    build_point_read,
)
from omnimarket.nodes.node_work_ledger_reader_effect.handlers.handler_work_ledger_reader import (
    HandlerWorkLedgerReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    EnumReaderQuery,
    EnumReaderStatus,
    ModelReaderRequest,
)
from omnimarket.nodes.node_work_ledger_reader_effect.overlay import load_overlay
from omnimarket.nodes.node_work_ledger_reader_effect.protocols import (
    ProtocolLedgerPointRead,
    ProtocolReaderEventSink,
)

MAX_WINDOW_HOURS = 24 * 30


class LedgerRowsUnreadableError(RuntimeError):
    """The reader did not answer the rows read with a fresh ``ok``; nothing was read."""


def read_window_rows(
    reader: str,
    window_hours: int,
    *,
    environ: Mapping[str, str] | None = None,
    point_read: ProtocolLedgerPointRead | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[str, ...]:
    """The node's rows of the last ``window_hours`` hours for the reader named ``reader``."""
    if not 1 <= window_hours <= MAX_WINDOW_HOURS:
        raise LedgerRowsUnreadableError(
            f"a rows window is 1 to {MAX_WINDOW_HOURS} hours; {window_hours} asked"
        )
    env = os.environ if environ is None else environ
    try:
        overlay = load_overlay(env)
    except NodeOverlayError as exc:
        raise LedgerRowsUnreadableError(f"reader overlay: {exc}") from exc
    journal = env.get(overlay.events.journal_env, "").strip()
    sink: ProtocolReaderEventSink = (
        JournalEventSink(Path(journal)) if journal else InMemoryEventSink()
    )
    handler = HandlerWorkLedgerReader(
        overlay,
        point_read if point_read is not None else build_point_read(overlay.route, env),
        sink,
        clock=clock,
    )
    result = handler.handle(
        ModelReaderRequest(
            reader=reader, query=EnumReaderQuery.ROWS, window_hours=window_hours
        )
    )
    if result.status is not EnumReaderStatus.OK:
        raise LedgerRowsUnreadableError(
            f"reader {reader}: {result.status.value} ({result.reason})"
        )
    return result.lines


__all__ = ["MAX_WINDOW_HOURS", "LedgerRowsUnreadableError", "read_window_rows"]
