# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Adapters from omnibase_spi ledger rows to the orchestrator's ledger seam."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Protocol

from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    FloorFillUnreadableError,
)

_CELL_SEPARATOR = " | "


class _RowsForLane(Protocol):
    def rows_for_lane(self, lane: str) -> tuple[str, ...]: ...


def _cells(row: str) -> dict[str, str]:
    cells: dict[str, str] = {}
    for cell in row.split(_CELL_SEPARATOR)[1:]:
        name, separator, value = cell.partition("=")
        if separator:
            cells[name.strip()] = value.strip()
    return cells


def _row_type(row: str) -> str:
    parts = row.split(_CELL_SEPARATOR)
    return parts[1].strip() if len(parts) > 1 else ""


class LedgerRowsFloorLedger:
    """Reads a lane's rows through ``ProtocolWorkLedgerStore.rows_for_lane`` and decides CLAIM and liveness.

    A CLAIM is found when a CLAIM row carries the lane name and the ``key=`` cell both (open or closed).
    A lane has ended when a TERMINAL row of the lane exists. A read that raises is an unread ledger.
    """

    def __init__(self, store: _RowsForLane) -> None:
        self._store = store

    def _rows(self, lane: str) -> tuple[str, ...]:
        try:
            return self._store.rows_for_lane(lane)
        except Exception as error:
            raise FloorFillUnreadableError(f"{type(error).__name__}") from error

    def claim_found(self, lane: str, key: str) -> bool:
        for row in self._rows(lane):
            cells = _cells(row)
            if (
                _row_type(row) == "CLAIM"
                and cells.get("lane") == lane
                and cells.get("key") == key
            ):
                return True
        return False

    def lane_ended(self, lane: str) -> bool:
        return any(
            _row_type(row) == "TERMINAL" and _cells(row).get("lane") == lane
            for row in self._rows(lane)
        )


class DirectoryFloorBriefs:
    """Reads a stopped lane's report under ``reports_root`` and writes each continuation's brief under ``brief_directory`` as ``<lane>.md``."""

    def __init__(self, reports_root: str, brief_directory: str) -> None:
        self._root = Path(reports_root)
        self._directory = Path(brief_directory)

    def report_text(self, report_path: str) -> str | None:
        path = (self._root / report_path).resolve()
        if not path.is_relative_to(self._root.resolve()):
            return None
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as error:
            raise FloorFillUnreadableError(type(error).__name__) from error

    def save_brief(self, lane: str, text: str) -> str:
        target = self._directory / f"{lane}.md"
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(dir=self._directory, suffix=".tmp")
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                stream.write(text)
            os.replace(temporary, target)
        except OSError as error:
            raise FloorFillUnreadableError(type(error).__name__) from error
        return str(target)


__all__: list[str] = ["DirectoryFloorBriefs", "LedgerRowsFloorLedger"]
