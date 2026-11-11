# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger file of record as a source of rows to replay (OMN-19998).

``replay_from_file`` reads the rows it re-emits from the ledger file and its archive
splits, the same files the parity check reads, and keys them by row id (sha256 of the
trimmed row). The reader is a port: the in-memory form holds rows for a test, the file
form reads the paths the overlay names.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.handlers.work_ledger_text import split_ledger_rows

_STAMP_LENGTH = len("2026-01-01T00:00:00Z")
_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class ProtocolLedgerFileRows(Protocol):
    """The rows of the ledger file of record, in file order."""

    def rows(self) -> Sequence[str]: ...


class InMemoryLedgerFileRows:
    def __init__(self, rows: Sequence[str]) -> None:
        self._rows = [row.strip() for row in rows]

    def rows(self) -> Sequence[str]:
        return list(self._rows)


class FileLedgerFileRows:
    """The ledger file and the archive splits beside it, read on each call."""

    def __init__(
        self, ledger_path: Path, archive_dir: Path | None, archive_glob: str | None
    ) -> None:
        if archive_dir is not None and not archive_glob:
            raise ValueError("an archive directory needs an archive_glob")
        self._ledger_path = ledger_path
        self._archive_dir = archive_dir
        self._archive_glob = archive_glob

    def rows(self) -> Sequence[str]:
        paths = [self._ledger_path]
        if self._archive_dir is not None and self._archive_glob:
            paths = sorted(self._archive_dir.glob(self._archive_glob)) + paths
        found: list[str] = []
        for path in paths:
            found.extend(split_ledger_rows(path.read_text(encoding="utf-8")))
        return [row.strip() for row in found]


def row_stamp(row: str) -> datetime | None:
    """The UTC stamp that opens a ledger row, or None when the row does not open with one."""
    try:
        return datetime.strptime(row[:_STAMP_LENGTH], _STAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return None


def failure_log_row_ids(path: Path) -> list[str]:
    """The row ids a dual write's failure log names (one JSON object per line), first seen first.

    The dual write fails open: a row whose emit failed is in the ledger file and named here, and
    nothing retries it. Feeding these ids to ``replay_from_file`` turns each logged drop into a
    retry record. Lines that are not a JSON object with a row_id are skipped, never guessed at; ids
    the ledger file of record does not hold (a copy's rows) come back as ``not_in_file``.
    """
    seen: dict[str, None] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        row_id = entry.get("row_id") if isinstance(entry, dict) else None
        if isinstance(row_id, str) and row_id:
            seen.setdefault(row_id, None)
    return list(seen)


def select_rows(
    source: ProtocolLedgerFileRows,
    *,
    row_ids: Sequence[str],
    since: datetime | None,
    until: datetime | None,
) -> tuple[list[str], list[str]]:
    """The rows the file holds for a selection, in file order, and the requested ids it does not hold."""
    held = source.rows()
    if row_ids:
        wanted = set(row_ids)
        chosen = [row for row in held if work_ledger_row_id(row) in wanted]
        found = {work_ledger_row_id(row) for row in chosen}
        return chosen, sorted(wanted - found)
    if since is None or until is None:
        raise ValueError("a window selection needs both ends")
    chosen = []
    for row in held:
        stamp = row_stamp(row)
        if stamp is not None and since <= stamp <= until:
            chosen.append(row)
    return chosen, []


__all__ = [
    "FileLedgerFileRows",
    "InMemoryLedgerFileRows",
    "ProtocolLedgerFileRows",
    "failure_log_row_ids",
    "row_stamp",
    "select_rows",
]
