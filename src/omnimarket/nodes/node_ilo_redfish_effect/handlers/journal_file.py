# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""File journal and ledger approval source of the iLO 4 Redfish effect (OMN-21835)."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from omnimarket.models.ilo_redfish.model_ilo_redfish_result import (
    ModelIloApproval,
    ModelIloJournalRecord,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters.rows_read import (
    LedgerRowsUnreadableError,
    read_row_at,
)

_DEFAULT_APPROVER = "operator"
CONSENT_READER = "ilo-consent"
CONSENT_KIND = "OPERATOR-CONSENT"

RowAtReader = Callable[[datetime, str], tuple[str, ...]]


def read_consent_row_at(at: datetime, kind: str) -> tuple[str, ...]:
    """The moved ledger reader's point read: every row stamped ``at`` of ``kind``, at any age."""
    return read_row_at(CONSENT_READER, at, kind)


class FileActionJournal:
    """One JSON-lines file per host under ``journal_dir``, appended under an exclusive lock and fsynced.

    The append returns only after the line is on disk, so an ``intent`` record written before a write
    survives a crash of the process that then sent the write.
    """

    def __init__(self, journal_dir: Path) -> None:
        self._dir = journal_dir

    def _path(self, host: str) -> Path:
        return self._dir / f"{host}.jsonl"

    def lookup(self, host: str, key: str) -> list[ModelIloJournalRecord]:
        path = self._path(host)
        if not path.is_file():
            return []
        records: list[ModelIloJournalRecord] = []
        with path.open("r", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH)
            try:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = ModelIloJournalRecord.model_validate_json(line)
                    except ValidationError:
                        # A line that does not parse is kept as evidence of a prior attempt: refuse
                        # to guess. The caller treats an unreadable journal as an attempt for the key.
                        raise JournalUnreadableError(host) from None
                    if record.key == key:
                        records.append(record)
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        return records

    def append(self, record: ModelIloJournalRecord) -> None:
        self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self._path(record.host)
        line = record.model_dump_json() + "\n"
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            try:
                os.write(fd, line.encode("utf-8"))
                os.fsync(fd)
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class JournalUnreadableError(Exception):
    """A journal line could not be parsed, so no write for the host may be assumed unattempted."""


class LedgerApprovalSource:
    """Reads an OPERATOR-CONSENT row from the rolling work ledger by its timestamp.

    The path is passed in by the composition root. The row shape is rule 18's:
    ``<ts> | OPERATOR-CONSENT | lane=<lane> | "<words>" | APPROVED SCOPE: ... | OUT OF SCOPE: ... | ...``.
    A row with an ``approved_by=`` cell names the approver; a plain consent row is the operator's.
    """

    def __init__(self, ledger_path: Path) -> None:
        self._path = ledger_path

    def find(self, ref: str) -> ModelIloApproval | None:
        prefix = f"{ref} | OPERATOR-CONSENT | "
        try:
            with self._path.open("r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if line.startswith(prefix):
                        return self.parse(ref, line.rstrip("\n"))
        except OSError:
            # An unreadable ledger is no approval: the caller refuses the write.
            return None
        return None

    @staticmethod
    def parse(ref: str, line: str) -> ModelIloApproval | None:
        cells = [cell.strip() for cell in line.split(" | ")]
        lane: str | None = None
        approved_by = _DEFAULT_APPROVER
        scope: str | None = None
        out_of_scope: str | None = None
        for cell in cells[2:]:
            if cell.startswith("lane="):
                lane = cell.removeprefix("lane=").split()[0]
            elif cell.startswith("approved_by="):
                approved_by = cell.removeprefix("approved_by=").split()[0]
            elif cell.startswith("APPROVED SCOPE:"):
                scope = cell.removeprefix("APPROVED SCOPE:").strip()
            elif cell.startswith("OUT OF SCOPE:"):
                out_of_scope = cell.removeprefix("OUT OF SCOPE:").strip()
        if not scope or not out_of_scope:
            return None
        try:
            at = datetime.strptime(ref, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        except ValueError:
            return None
        return ModelIloApproval(
            ref=ref,
            at=at,
            lane=lane,
            approved_by=approved_by,
            approved_scope=scope,
            out_of_scope=out_of_scope,
        )


class ReaderApprovalSource:
    """Reads an OPERATOR-CONSENT row by its timestamp through the moved ledger reader's point read.

    The lookup is fail-closed: an unreadable answer (the reader is not moved, the node did not
    answer, the answer is stale), an absent row and an ambiguous answer (more than one consent row
    at that instant) all answer no approval, so the caller refuses the write. The approval's age is
    judged by the handler, not here. The row shape is the one :class:`LedgerApprovalSource` parses.
    """

    def __init__(self, row_at: RowAtReader = read_consent_row_at) -> None:
        self._row_at = row_at

    def find(self, ref: str) -> ModelIloApproval | None:
        try:
            at = datetime.strptime(ref, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        except ValueError:
            return None
        try:
            rows = self._row_at(at, CONSENT_KIND)
        except LedgerRowsUnreadableError:
            return None
        prefix = f"{ref} | {CONSENT_KIND} | "
        matching = [row for row in rows if row.startswith(prefix)]
        if len(matching) != 1:
            return None
        return LedgerApprovalSource.parse(ref, matching[0].rstrip("\n"))


__all__ = [
    "CONSENT_KIND",
    "CONSENT_READER",
    "FileActionJournal",
    "JournalUnreadableError",
    "LedgerApprovalSource",
    "ReaderApprovalSource",
    "RowAtReader",
    "read_consent_row_at",
]
