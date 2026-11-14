# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Condition adapters for node_lab_alarm: census sources, run record readers, and in-memory test doubles.

Census sources read a JSON document from a file or a subprocess command. Run record readers
extract outcomes from JSON lines files. All failures are named by exception type only; no
credential, URL, or raw output is leaked.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from omnimarket.nodes.node_lab_alarm.protocols import (
    CensusUnreadableError,
    ReceiptAbsentError,
    ReceiptMalformedError,
    ReceiptSurfaceUnreadableError,
    RunRecordUnreadableError,
    ShaUnresolvedError,
)

__all__ = [
    "CommandCensusSource",
    "FileCensusSource",
    "FileRunRecordReader",
    "InMemoryCensusSource",
    "InMemoryDeliveredShaSource",
    "InMemoryLabPassReceiptSource",
    "InMemoryRunRecordReader",
    "run_command",
]


def run_command(
    argv: Sequence[str], timeout_seconds: float
) -> subprocess.CompletedProcess[str]:
    """Run a command without a shell, capturing output as text."""
    return subprocess.run(  # no-contract-check: the lab census snapshot command, argv declared by the overlay
        list(argv),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout_seconds,
    )


class FileCensusSource:
    """Reads a census document from a JSON file.

    The file must contain a JSON object. A missing or unreadable file, non-JSON text, or a
    non-object document raises ``CensusUnreadableError`` naming the cause's type only.
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> Mapping[str, Any]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError as exc:
            raise CensusUnreadableError(
                f"the census file is unreadable ({type(exc).__name__})"
            ) from exc
        try:
            document = json.loads(text)
        except ValueError as exc:
            raise CensusUnreadableError("the census file is not JSON") from exc
        if not isinstance(document, dict):
            raise CensusUnreadableError("the census document is not a JSON object")
        return document


class CommandCensusSource:
    """Runs a command to obtain a census document.

    The command is run without a shell. A nonzero exit code, a timeout, an ``OSError``, or
    output that is not a JSON object raises ``CensusUnreadableError`` naming the exit code
    or the cause's type, never the output.
    """

    def __init__(
        self,
        argv: Sequence[str],
        timeout_seconds: float,
        run: Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]
        | None = None,
    ) -> None:
        self._argv = list(argv)
        self._timeout_seconds = timeout_seconds
        self._run = run or run_command

    def read(self) -> Mapping[str, Any]:
        try:
            completed = self._run(self._argv, self._timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            raise CensusUnreadableError(
                f"the census command timed out ({type(exc).__name__})"
            ) from exc
        except OSError as exc:
            raise CensusUnreadableError(
                f"the census command failed ({type(exc).__name__})"
            ) from exc
        if completed.returncode != 0:
            raise CensusUnreadableError(
                f"the census command exited with code {completed.returncode}"
            )
        try:
            document = json.loads(completed.stdout)
        except ValueError as exc:
            raise CensusUnreadableError(
                "the census command's output is not JSON"
            ) from exc
        if not isinstance(document, dict):
            raise CensusUnreadableError(
                "the census command's output is not a JSON object"
            )
        return document


class FileRunRecordReader:
    """Reads run outcomes from JSON-lines files, one file per condition.

    A condition with no path in the mapping raises ``RunRecordUnreadableError``. Only the last ``tail_bytes`` of a file are read. A missing
    file means no runs on record and returns an empty tuple. Each non-empty line must be
    JSON with a ``reports`` list of objects carrying ``condition`` and ``outcome``; the
    outcomes for the requested condition are returned newest-first, limited to ``count``.
    A malformed line or an ``OSError`` other than a missing file raises
    ``RunRecordUnreadableError``.
    """

    def __init__(self, paths: Mapping[str, Path], tail_bytes: int) -> None:
        self._paths = paths
        self._tail_bytes = tail_bytes

    def recent_outcomes(self, condition: str, count: int) -> tuple[str, ...]:
        try:
            path = self._paths[condition]
        except KeyError:
            raise RunRecordUnreadableError(
                f"no path is declared for condition {condition!r}"
            ) from None
        try:
            with path.open("rb") as handle:
                size = handle.seek(0, 2)
                start = max(0, size - self._tail_bytes)
                handle.seek(start)
                raw = handle.read()
        except FileNotFoundError:
            return ()
        except OSError as exc:
            raise RunRecordUnreadableError(
                f"the run record file is unreadable ({type(exc).__name__})"
            ) from exc
        try:
            text = raw.decode("utf-8", errors="replace")
        except ValueError as exc:
            raise RunRecordUnreadableError("the run record file is not text") from exc
        lines = text.splitlines()
        if start > 0 and lines:
            # The window opens mid-line: the first line is partial and is not read.
            lines = lines[1:]
        outcomes: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
            except ValueError as exc:
                raise RunRecordUnreadableError("a run record line is not JSON") from exc
            if not isinstance(record, dict) or "reports" not in record:
                raise RunRecordUnreadableError("a run record line has no reports list")
            reports = record["reports"]
            if not isinstance(reports, list):
                raise RunRecordUnreadableError("the reports field is not a list")
            for report in reports:
                if not isinstance(report, dict):
                    continue
                if report.get("condition") == condition:
                    outcome = report.get("outcome")
                    if isinstance(outcome, str):
                        outcomes.append(outcome)
        return tuple(reversed(outcomes[-count:])) if count > 0 else ()


class InMemoryCensusSource:
    """Serves one census document, or refuses as a surface that cannot be read."""

    def __init__(
        self,
        document: Mapping[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        self.document = document
        self.error = error
        self.reads = 0

    def read(self) -> Mapping[str, Any]:
        self.reads += 1
        if self.error is not None or self.document is None:
            raise CensusUnreadableError(self.error or "no census document")
        return self.document


class InMemoryRunRecordReader:
    """Serves run outcomes per condition (newest first), or refuses as a record that cannot be read."""

    def __init__(
        self,
        outcomes: Mapping[str, Sequence[str]] | None = None,
        error: str | None = None,
    ) -> None:
        self.outcomes: Mapping[str, Sequence[str]] = outcomes or {}
        self.error = error

    def recent_outcomes(self, condition: str, count: int) -> tuple[str, ...]:
        if self.error is not None:
            raise RunRecordUnreadableError(self.error)
        return tuple(self.outcomes.get(condition, ())[:count])


class InMemoryDeliveredShaSource:
    """Serves a delivered SHA and its surface, or refuses as an unresolved SHA."""

    def __init__(
        self,
        sha: str | None = None,
        surface: str = "inmemory",
        error: str | None = None,
    ) -> None:
        self.sha = sha
        self.surface = surface
        self.error = error

    def read(self) -> tuple[str, str]:
        if self.error is not None:
            raise ShaUnresolvedError(self.error)
        if self.sha is None:
            raise ShaUnresolvedError("no SHA is delivered")
        return (self.sha, self.surface)


class InMemoryLabPassReceiptSource:
    """Serves lab pass receipts by SHA, or refuses with a specific error.

    When ``error`` is set, raises ``ReceiptSurfaceUnreadableError``. When ``malformed`` is
    set, raises ``ReceiptMalformedError``. When the SHA is not in ``documents``, raises
    ``ReceiptAbsentError``. Otherwise returns the document.
    """

    def __init__(
        self,
        documents: Mapping[str, Mapping[str, Any]] | None = None,
        error: str | None = None,
        malformed: str | None = None,
    ) -> None:
        self.documents = documents or {}
        self.error = error
        self.malformed = malformed

    def read(self, sha: str) -> Mapping[str, Any]:
        if self.error is not None:
            raise ReceiptSurfaceUnreadableError(self.error)
        if self.malformed is not None:
            raise ReceiptMalformedError(self.malformed)
        if sha not in self.documents:
            raise ReceiptAbsentError(f"no receipt for SHA {sha!r}")
        return self.documents[sha]
