# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The injected seam: how the ledger's lane writes are read. In memory by default; a file reader is an effect-node backend."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import Enum
from typing import Protocol

from omnimarket.nodes.node_floor_fill_owner_count_compute.models import (
    ModelFloorFillLaneWrites,
    ModelFloorFillLedgerRow,
)

_ROW = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) \| ([A-Z][A-Z-]*) \| (.*)$")
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


class LaneWriteReadError(RuntimeError):
    """The ledger could not be read at all; the count is unknown, not zero."""


class EnumLaneWriteBreak(Enum):
    """The named injected breaks of the reading, each one a way the real read goes wrong."""

    LEDGER_UNREADABLE = "ledger_unreadable"
    LEDGER_SILENT = "ledger_silent"


class ProtocolLaneWriteReader(Protocol):
    def read(self, since: datetime, until: datetime) -> ModelFloorFillLaneWrites: ...


def parse_ledger_text(
    text: str, since: datetime, until: datetime
) -> ModelFloorFillLaneWrites:
    """Rows of ``[since, until]`` that carry a ``lane=`` cell; a line that is not a ledger row is skipped and counted.

    The ledger is appended in arrival order, not timestamp order, so every line is tested against the window.
    """
    rows: list[ModelFloorFillLedgerRow] = []
    read = skipped = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        match = _ROW.match(line)
        if match is None:
            skipped += 1
            continue
        at = datetime.strptime(match.group(1), _STAMP).replace(tzinfo=UTC)
        if not since <= at <= until:
            continue
        read += 1
        lane = next(
            (
                cell[5:].strip()
                for cell in match.group(3).split(" | ")
                if cell.startswith("lane=")
            ),
            "",
        )
        if lane:
            rows.append(
                ModelFloorFillLedgerRow(
                    at=at, kind=match.group(2), lane=lane.split()[0]
                )
            )
    return ModelFloorFillLaneWrites(
        rows=tuple(rows), lines_read=read, lines_skipped=skipped
    )


class InMemoryLaneWriteReader:
    """The default reader: ledger text held in memory, with an optional named injected break."""

    def __init__(
        self, text: str = "", inject_break: EnumLaneWriteBreak | None = None
    ) -> None:
        self._text = text
        self._break = inject_break

    def read(self, since: datetime, until: datetime) -> ModelFloorFillLaneWrites:
        if self._break is EnumLaneWriteBreak.LEDGER_UNREADABLE:
            raise LaneWriteReadError("injected break ledger_unreadable")
        if self._break is EnumLaneWriteBreak.LEDGER_SILENT:
            return ModelFloorFillLaneWrites()
        return parse_ledger_text(self._text, since, until)
