# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus implementation of the ledger-rows port: the rolling ledger through the reader node.

Every read goes to ``node_work_ledger_reader_effect`` (the bus-fed query node), named as the
``handoff-check`` reader, so the overlay's ``moved_readers`` decides whether it answers. The
reader caps one rows window at 720 hours; every scan the handoff makes is inside it (the widest
declared scan is 72 hours; a hold or open claim older than 30 days is outside the window and is
not listed). A failed or not-moved read is typed, never an empty answer and never a file read.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from omnimarket.models.model_work_ledger_reader import (
    EnumReaderQuery,
    ModelReaderRequest,
)
from omnimarket.nodes.node_handoff_check_effect.models import ModelLedgerRowsRead
from omnimarket.nodes.node_work_ledger_reader_effect.adapters import (
    InMemoryEventSink,
    build_point_read,
)
from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
    HandlerWorkLedgerReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.overlay import load_overlay

LEDGER_ROWS_READER = "handoff-check"
MAX_WINDOW_HOURS = 24 * 30
_VIEWS = {"inbox": EnumReaderQuery.INBOX, "holds": EnumReaderQuery.HOLDS}


class BusLedgerRows:
    """``ProtocolLedgerRows`` over one ``HandlerWorkLedgerReader``."""

    def __init__(self, reader: HandlerWorkLedgerReader) -> None:
        self._reader = reader

    def read_rows(self, window_hours: int) -> ModelLedgerRowsRead:
        return self._read(
            ModelReaderRequest(
                reader=LEDGER_ROWS_READER,
                query=EnumReaderQuery.ROWS,
                window_hours=max(1, min(window_hours, MAX_WINDOW_HOURS)),
            )
        )

    def read_view(self, view: str, lane: str | None) -> ModelLedgerRowsRead:
        query = _VIEWS.get(view)
        if query is None:
            return ModelLedgerRowsRead(
                status="unreadable", reason=f"unknown-view-{view}"
            )
        if query is EnumReaderQuery.INBOX and not lane:
            return ModelLedgerRowsRead(status="unreadable", reason="inbox-needs-a-lane")
        return self._read(
            ModelReaderRequest(
                reader=LEDGER_ROWS_READER,
                query=query,
                lane=lane if query is EnumReaderQuery.INBOX else None,
            )
        )

    def _read(self, request: ModelReaderRequest) -> ModelLedgerRowsRead:
        result = self._reader.handle(request)
        return ModelLedgerRowsRead(
            status=result.status.value,
            lines=result.lines,
            reason=result.reason,
            watermark_age_s=result.watermark_age_s,
        )


def build_bus_ledger_rows(environ: Mapping[str, str] | None = None) -> BusLedgerRows:
    """The port the CLI and the deployed handler use: the reader node's own overlay and route."""
    env = os.environ if environ is None else environ
    overlay = load_overlay(env)
    return BusLedgerRows(
        HandlerWorkLedgerReader(
            overlay, build_point_read(overlay.route, env), InMemoryEventSink()
        )
    )


__all__ = [
    "LEDGER_ROWS_READER",
    "MAX_WINDOW_HOURS",
    "BusLedgerRows",
    "build_bus_ledger_rows",
]
