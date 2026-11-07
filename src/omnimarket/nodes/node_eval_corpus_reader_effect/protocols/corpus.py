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
        """Entries (all when ``run_ids`` is empty) and the count of unreadable journal lines."""
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
    """Append-only journal. A line that does not parse is skipped and counted, never silently fixed."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def append(self, entry: ModelEvalCorpusEntry) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(entry.model_dump_json() + "\n")

    def read(
        self, run_ids: tuple[str, ...]
    ) -> tuple[tuple[ModelEvalCorpusEntry, ...], int]:
        try:
            text = self._path.read_text(encoding="utf-8")
        except OSError:
            return (), 0
        entries: dict[str, ModelEvalCorpusEntry] = {}
        skipped = 0
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                entry = ModelEvalCorpusEntry.model_validate(json.loads(line))
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
