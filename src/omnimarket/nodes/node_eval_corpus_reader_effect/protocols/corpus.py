# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Ports and adapters of the eval corpus (OMN-22978).

The default backend is in memory. ``JsonlEvalCorpus`` is the real backend: an append-only journal beside the
other floor journals, chosen by overlay. The two sources of the join (delegated results, cost rows) are ports:
piece 15's projections are the real adapters, wired by overlay in a later change; the in-memory sources here
stand in for them in tests.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from omnimarket.models.eval_corpus import (
    ModelDelegatedResult,
    ModelDelegationCost,
    ModelEvalCorpusEntry,
)


class ProtocolDelegatedResults(Protocol):
    def read_results(
        self, run_ids: tuple[str, ...]
    ) -> tuple[ModelDelegatedResult, ...]:
        """The results piece 15 holds for the named runs; a run it does not hold is simply absent."""
        ...


class ProtocolDelegationCosts(Protocol):
    def read_cost(self, run_id: str) -> ModelDelegationCost | None:
        """The cost row for a run, or ``None`` when none exists."""
        ...


class ProtocolEvalCorpus(Protocol):
    def append(self, entry: ModelEvalCorpusEntry) -> None: ...

    def read(
        self, run_ids: tuple[str, ...]
    ) -> tuple[tuple[ModelEvalCorpusEntry, ...], int]:
        """Entries (all when ``run_ids`` is empty) and the count of unreadable journal lines.

        An absent journal is an empty corpus; storage that cannot be read raises ``OSError``.
        """
        ...


class InMemoryDelegatedResults:
    def __init__(self, results: Iterable[ModelDelegatedResult] = ()) -> None:
        self._results = {r.run_id: r for r in results}

    def read_results(
        self, run_ids: tuple[str, ...]
    ) -> tuple[ModelDelegatedResult, ...]:
        return tuple(self._results[i] for i in run_ids if i in self._results)


class InMemoryDelegationCosts:
    def __init__(self, costs: Iterable[ModelDelegationCost] = ()) -> None:
        self._costs = {c.run_id: c for c in costs}

    def read_cost(self, run_id: str) -> ModelDelegationCost | None:
        return self._costs.get(run_id)


class InMemoryEvalCorpus:
    def __init__(self) -> None:
        self._entries: dict[str, ModelEvalCorpusEntry] = {}

    def append(self, entry: ModelEvalCorpusEntry) -> None:
        self._entries[entry.run_id] = entry

    def read(
        self, run_ids: tuple[str, ...]
    ) -> tuple[tuple[ModelEvalCorpusEntry, ...], int]:
        return _select(self._entries, run_ids), 0


class JsonlEvalCorpus:
    """Append-only journal. A line that does not parse or decode is skipped and counted, never silently fixed.

    Only a journal that does not exist reads as an empty corpus; any other read failure (permissions, a directory at
    the path, a filesystem error) raises ``OSError``, so unreadable storage is not mistaken for "no entries".
    """

    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, entry: ModelEvalCorpusEntry) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        record = (entry.model_dump_json() + "\n").encode("utf-8")
        with self._path.open("a+b") as handle:
            size = handle.seek(0, os.SEEK_END)
            if size:
                handle.seek(size - 1)
                # A torn tail (an interrupted write) has no newline: end it, so it stays one skipped line and the
                # new record is not concatenated onto it.
                if handle.read(1) != b"\n":
                    record = b"\n" + record
            handle.write(record)

    def read(
        self, run_ids: tuple[str, ...]
    ) -> tuple[tuple[ModelEvalCorpusEntry, ...], int]:
        try:
            data = self._path.read_bytes()
        except FileNotFoundError:
            return (), 0
        entries: dict[str, ModelEvalCorpusEntry] = {}
        skipped = 0
        # Split on the newline byte and decode per record, so one undecodable record is counted and the rest read.
        for raw in data.split(b"\n"):
            if not raw.strip():
                continue
            try:
                entry = ModelEvalCorpusEntry.model_validate(
                    json.loads(raw.decode("utf-8"))
                )
            except (ValueError, ValidationError):
                skipped += 1
                continue
            entries[entry.run_id] = entry
        return _select(entries, run_ids), skipped


def _select(
    entries: dict[str, ModelEvalCorpusEntry], run_ids: tuple[str, ...]
) -> tuple[ModelEvalCorpusEntry, ...]:
    if not run_ids:
        return tuple(entries.values())
    return tuple(entries[i] for i in run_ids if i in entries)


__all__ = [
    "InMemoryDelegatedResults",
    "InMemoryDelegationCosts",
    "InMemoryEvalCorpus",
    "JsonlEvalCorpus",
    "ProtocolDelegatedResults",
    "ProtocolDelegationCosts",
    "ProtocolEvalCorpus",
]
