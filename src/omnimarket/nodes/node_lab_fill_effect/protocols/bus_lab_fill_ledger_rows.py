# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The bus-fed ledger reader the lab-fill select step reads its rolling ledger through (OMN-19998).

It asks the bus work ledger query node for a window of rows and returns the node's typed answer; it
reads no file and falls back to nothing.
"""

from __future__ import annotations

from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
    HandlerWorkLedgerReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    EnumReaderQuery,
    ModelReaderRequest,
)

from .protocol_lab_fill_ledger_rows import LabFillLedgerRows


class BusLabFillLedgerRows:
    """Reads the rolling ledger's window rows through the bus work ledger query node."""

    def __init__(self, reader: HandlerWorkLedgerReader) -> None:
        self._reader = reader

    def read_rows(self, reader: str, window_hours: int) -> LabFillLedgerRows:
        result = self._reader.handle(
            ModelReaderRequest(
                reader=reader,
                query=EnumReaderQuery.ROWS,
                window_hours=window_hours,
            )
        )
        return LabFillLedgerRows(
            status=result.status.value,
            lines=result.lines,
            reason=result.reason,
            watermark_age_s=result.watermark_age_s,
        )
