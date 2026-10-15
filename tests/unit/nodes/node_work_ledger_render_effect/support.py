# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fixtures shared by the markdown projection tests (OMN-20275, M6 deliverable 2)."""

from __future__ import annotations

from pathlib import Path

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.nodes.node_work_ledger_render_effect.handlers.handler_work_ledger_render import (
    HandlerWorkLedgerRender,
)
from omnimarket.nodes.node_work_ledger_render_effect.handlers.rendered_ledger_file import (
    create_baseline_file,
)
from omnimarket.nodes.node_work_ledger_render_effect.models.model_work_ledger_render import (
    ModelWorkLedgerRenderRow,
)
from omnimarket.protocols.protocol_work_ledger_record_source import (
    WorkLedgerSourceError,
)

LEDGER_ID = "rolling-work-ledger"


def row(seq: int, text: str | None = None) -> ModelWorkLedgerRenderRow:
    raw = text or f"2026-10-14T10:{seq:02d}:00Z | STATUS | lane=l{seq} | row {seq}"
    return ModelWorkLedgerRenderRow(
        ledger_id=LEDGER_ID, ledger_seq=seq, row_id=work_ledger_row_id(raw), raw_row=raw
    )


class FakeSource:
    """The ledger of record: rows by seq, a head, and switchable faults."""

    def __init__(self, rows: dict[int, ModelWorkLedgerRenderRow]) -> None:
        self.rows = dict(rows)
        self.head_override: int | None = None
        self.down = False
        self.row_reads: list[int] = []

    def read_row(
        self, *, ledger_id: str, ledger_seq: int
    ) -> ModelWorkLedgerRenderRow | None:
        if self.down:
            raise WorkLedgerSourceError("source unreachable")
        self.row_reads.append(ledger_seq)
        return self.rows.get(ledger_seq)

    def read_head(self, *, ledger_id: str) -> int:
        if self.down:
            raise WorkLedgerSourceError("source unreachable")
        if self.head_override is not None:
            return self.head_override
        return max(self.rows, default=0)


def make_file(tmp_path: Path, baseline: int = 0) -> Path:
    path = tmp_path / "ledger.md"
    create_baseline_file(path, baseline_seq=baseline)
    return path


def make_node(path: Path, source: FakeSource) -> HandlerWorkLedgerRender:
    return HandlerWorkLedgerRender(path=path, source=source, ledger_id=LEDGER_ID)


def file_seq_rows(path: Path) -> list[str]:
    return path.read_text().splitlines()[1:]
