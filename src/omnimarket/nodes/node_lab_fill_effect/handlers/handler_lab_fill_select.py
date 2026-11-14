# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab-fill select step: read the rolling ledger from the bus, then select (OMN-19998).

A not-ok ledger read (stale, unreadable or not-moved) returns a typed result and never runs the
selection: a stale, unreadable or empty-by-failure ledger would make every ticket look never
attempted.
"""

from __future__ import annotations

from omnimarket.nodes.node_lab_fill_selection_compute.handlers.handler_lab_fill_selection import (
    HandlerLabFillSelection,
)
from omnimarket.nodes.node_lab_fill_selection_compute.models import (
    ModelLabFillSelectionRequest,
)

from ..models.model_lab_fill_select import (
    ModelLabFillSelectRequest,
    ModelLabFillSelectResult,
)
from ..protocols.protocol_lab_fill_ledger_rows import ProtocolLabFillLedgerRows


class HandlerLabFillSelect:
    """Reads the ledger window from the bus reader, then runs the selection on its rows."""

    def __init__(
        self,
        rows: ProtocolLabFillLedgerRows,
        selector: HandlerLabFillSelection | None = None,
    ) -> None:
        self._rows = rows
        self._selector = selector if selector is not None else HandlerLabFillSelection()

    def handle(self, request: ModelLabFillSelectRequest) -> ModelLabFillSelectResult:
        ledger = self._rows.read_rows(
            request.ledger_reader, request.ledger_window_hours
        )
        if ledger.status != "ok":
            return ModelLabFillSelectResult(
                ledger_status=ledger.status,
                ledger_reason=ledger.reason,
                ledger_rows=len(ledger.lines),
                ledger_watermark_age_s=ledger.watermark_age_s,
                selection=None,
            )
        selection = ModelLabFillSelectionRequest.model_validate(
            {**request.selection, "ledger_lines": ledger.lines}
        )
        return ModelLabFillSelectResult(
            ledger_status="ok",
            ledger_reason=ledger.reason,
            ledger_rows=len(ledger.lines),
            ledger_watermark_age_s=ledger.watermark_age_s,
            selection=self._selector.handle(selection),
        )
