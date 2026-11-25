# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A ledger-rows port stub: fixed lines and a record of every read; no file, no bus."""

from __future__ import annotations

from omnimarket.nodes.node_handoff_check_effect.models import ModelLedgerRowsRead


class StubRows:
    def __init__(self, *lines: str, status: str = "ok", reason: str = "") -> None:
        self.lines = lines
        self.status = status
        self.reason = reason
        self.windows: list[int] = []
        self.views: list[tuple[str, str | None]] = []

    def read_rows(self, window_hours: int) -> ModelLedgerRowsRead:
        self.windows.append(window_hours)
        return ModelLedgerRowsRead(
            status=self.status, lines=self.lines, reason=self.reason
        )

    def read_view(self, view: str, lane: str | None) -> ModelLedgerRowsRead:
        self.views.append((view, lane))
        return ModelLedgerRowsRead(
            status=self.status, lines=(f"{view} answered",), reason=self.reason
        )
