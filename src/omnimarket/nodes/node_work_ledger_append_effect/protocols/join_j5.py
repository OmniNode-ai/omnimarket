# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports and in-memory adapters of the J5 join (OMN-22914).

The default backend is in memory: a durable text, a projection that hears each appended row the way the bus fan-out
would, and a store of identities. ``ScratchFileJoinLedger`` is the same durable text as a scratch file, and refuses the
live ledger file's name. A real backend is another adapter of the same three ports, chosen by overlay.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from omnimarket.handlers.ledger_row_grammar import GOVERNED_LEDGER_NAMES
from omnimarket.handlers.work_ledger_fold import WorkLedgerFoldError, fold_row
from omnimarket.models.model_work_ledger_fold_request import ModelWorkLedgerFoldRequest
from omnimarket.models.model_work_ledger_fold_result import ModelWorkLedgerRowRecord
from omnimarket.nodes.node_work_ledger_append_effect.protocols.local_ledger_append_command import (
    ModelAppendCommandResult,
)

_ROW_START = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z \| ")

#: File names that are the live ledger, taken from the row grammar that governs it; a scratch lane never takes one.
LIVE_LEDGER_FILE_NAMES = GOVERNED_LEDGER_NAMES


class StoreIdentityUnresolvedError(LookupError):
    """The store holds no identity for the reference, or could not be read."""


class ProtocolStoreIdentities(Protocol):
    """Piece 5's side of the join: the store gives the broker principal an append runs under."""

    def resolve(self, identity_ref: str) -> str:
        """The principal, or raise ``StoreIdentityUnresolvedError``."""
        ...


class ProtocolProjectedRows(Protocol):
    """Piece 6's projection, read by position."""

    def read_row(
        self, *, ledger_id: str, ledger_seq: int
    ) -> ModelWorkLedgerRowRecord | None: ...


class InMemoryStoreIdentities:
    def __init__(self, identities: Mapping[str, str]) -> None:
        self._identities = dict(identities)

    def resolve(self, identity_ref: str) -> str:
        try:
            return self._identities[identity_ref]
        except KeyError:
            raise StoreIdentityUnresolvedError(
                f"the store holds no identity for {identity_ref}"
            ) from None


class InMemoryProjectedRows:
    """The projection: folds a row event with the core fold and keeps the row record by position."""

    def __init__(self) -> None:
        self._rows: dict[tuple[str, int], ModelWorkLedgerRowRecord] = {}

    def project(self, *, raw_row: str, ledger_id: str, ledger_seq: int) -> None:
        try:
            result = fold_row(
                ModelWorkLedgerFoldRequest(
                    raw_row=raw_row,
                    ledger_id=ledger_id,
                    source="join-j5",
                    ledger_seq=ledger_seq,
                )
            )
        except WorkLedgerFoldError:
            return
        self._rows[(ledger_id, ledger_seq)] = result.row

    def read_row(
        self, *, ledger_id: str, ledger_seq: int
    ) -> ModelWorkLedgerRowRecord | None:
        return self._rows.get((ledger_id, ledger_seq))

    def rows(self) -> tuple[ModelWorkLedgerRowRecord, ...]:
        return tuple(self._rows.values())


def logical_rows(text: str) -> list[tuple[int, str]]:
    """Each logical row of ``text`` as ``(line number of its header, row)``.

    A logical row is a stamp line plus its continuation lines, blank lines dropped and the whole trimmed: the row
    shape the ledger's own splitter hands the projection, so the durable text and the projected row are compared
    in one representation."""
    groups: list[tuple[int, list[str]]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if _ROW_START.match(line):
            groups.append((number, [line]))
        elif groups and line.strip():
            groups[-1][1].append(line)
    return [(number, "\n".join(lines).strip()) for number, lines in groups]


def _project_appended(
    projection: InMemoryProjectedRows, ledger_id: str, text: str, rows: str
) -> None:
    """Tell the projection each appended logical row at its position, as the row event would."""
    first = len(text.splitlines()) - len(rows.splitlines())
    for number, raw_row in logical_rows(rows):
        projection.project(
            raw_row=raw_row, ledger_id=ledger_id, ledger_seq=first + number
        )


class InMemoryJoinLedger:
    """Durable text in memory, as both the append runner and the ledger reader of the append handler."""

    def __init__(
        self,
        *,
        projection: InMemoryProjectedRows,
        ledger_id: str = "rolling-work-ledger",
        header: str = "# In-memory ledger\n",
    ) -> None:
        self._projection = projection
        self._ledger_id = ledger_id
        self._text = header
        self.append_calls: list[str] = []

    def read_text(self) -> str:
        return self._text

    def append(self, rows: str) -> ModelAppendCommandResult:
        self.append_calls.append(rows)
        if self._text and not self._text.endswith("\n"):
            self._text += "\n"
        self._text += rows if rows.endswith("\n") else rows + "\n"
        _project_appended(self._projection, self._ledger_id, self._text, rows)
        return ModelAppendCommandResult(exit_code=0)


class ScratchFileJoinLedger:
    """A scratch lane: the durable text is a file the caller names, never the live ledger."""

    def __init__(
        self,
        path: Path,
        *,
        projection: InMemoryProjectedRows,
        ledger_id: str = "rolling-work-ledger",
    ) -> None:
        # A link is followed: a scratch name that resolves to the live file is the live file.
        for name in (path.name, path.resolve().name):
            if name in LIVE_LEDGER_FILE_NAMES:
                raise ValueError(
                    f"{name} is the live ledger file: a scratch lane never appends to it"
                )
        self._path = path
        self._projection = projection
        self._ledger_id = ledger_id
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# Scratch lane ledger\n", encoding="utf-8")

    def read_text(self) -> str:
        return self._path.read_text(encoding="utf-8")

    def append(self, rows: str) -> ModelAppendCommandResult:
        existing = self.read_text()
        with self._path.open("a", encoding="utf-8") as stream:
            if existing and not existing.endswith("\n"):
                stream.write("\n")
            stream.write(rows if rows.endswith("\n") else rows + "\n")
        _project_appended(self._projection, self._ledger_id, self.read_text(), rows)
        return ModelAppendCommandResult(exit_code=0)


__all__ = [
    "LIVE_LEDGER_FILE_NAMES",
    "InMemoryJoinLedger",
    "InMemoryProjectedRows",
    "InMemoryStoreIdentities",
    "ProtocolProjectedRows",
    "ProtocolStoreIdentities",
    "ScratchFileJoinLedger",
    "StoreIdentityUnresolvedError",
    "logical_rows",
]
