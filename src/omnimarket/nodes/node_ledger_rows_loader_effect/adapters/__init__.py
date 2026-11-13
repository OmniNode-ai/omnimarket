# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Event sinks and the file rows source of the ledger rows loader."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from omnimarket.nodes.node_ledger_rows_loader_effect.models import (
    EnumRowsSource,
    ModelRowsLoaderEvent,
)

_ROW_OPEN = re.compile(
    r"^(?:[-*] +)?\|? *(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})(?::\d{2})?Z\s*\|"
)
_SPLIT_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})-split")
_ROLL_MARKER = "<!-- ledger-roll-archive"


class InMemoryRowsLoaderSink:
    """Keeps the events in a list; the default and the tests' sink."""

    def __init__(self) -> None:
        self.events: list[ModelRowsLoaderEvent] = []

    def emit(self, event: ModelRowsLoaderEvent) -> None:
        self.events.append(event)

    def fallback_reads(self) -> int:
        return sum(1 for e in self.events if e.source is EnumRowsSource.FILE_FALLBACK)


class JournalRowsLoaderSink:
    """Appends each event as one JSON line; the fallback count is the journal's file-fallback lines."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def emit(self, event: ModelRowsLoaderEvent) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(event.model_dump_json() + "\n")

    def fallback_reads(self) -> int:
        if not self._path.is_file():
            return 0
        count = 0
        for line in self._path.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines():
            if not line.strip():
                continue
            try:
                if json.loads(line).get("source") == EnumRowsSource.FILE_FALLBACK.value:
                    count += 1
            except ValueError:
                continue
        return count


class FileRowsReader:
    """The rows of the window from the ledger file and the archive splits the glob names (the fallback source)."""

    def __init__(
        self, ledger: Path, since: datetime, archive_glob: str | None = None
    ) -> None:
        self._ledger = ledger
        self._since = since
        self._archive_glob = archive_glob

    def _files(self) -> list[Path]:
        files: list[Path] = []
        if self._archive_glob is None:
            return [self._ledger]
        archive = self._ledger.parent / "archive"
        for path in sorted(archive.glob(self._archive_glob)):
            match = _SPLIT_DATE.search(path.name)
            if match and match.group(1) >= self._since.date().isoformat():
                files.append(path)
        return [*files, self._ledger]

    def read(self) -> tuple[str, ...]:
        if not self._ledger.is_file():
            raise FileNotFoundError(str(self._ledger))
        floor = self._since.strftime("%Y-%m-%dT%H:%M")
        rows: list[tuple[str, ...]] = []
        for path in self._files():
            current: list[str] | None = None
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                opened = _ROW_OPEN.match(line)
                if opened:
                    if current is not None:
                        rows.append(tuple(current))
                    current = [line] if opened.group(1) >= floor else None
                elif (
                    line.startswith(_ROLL_MARKER)
                    or not line.strip()
                    or line.startswith("#")
                ):
                    continue
                elif current is not None:
                    current.append(line)
            if current is not None:
                rows.append(tuple(current))
        seen: set[tuple[str, ...]] = set()
        lines: list[str] = []
        for row in rows:
            if row in seen:
                continue
            seen.add(row)
            lines.extend(row)
        return tuple(lines)


__all__ = ["FileRowsReader", "InMemoryRowsLoaderSink", "JournalRowsLoaderSink"]
