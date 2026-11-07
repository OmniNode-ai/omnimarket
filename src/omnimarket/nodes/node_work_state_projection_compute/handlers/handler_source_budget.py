# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read one source of the work-state projection under its own budget; serve its last snapshot stale when it is over.

A reader runs on a worker thread. The caller waits at most the source's `timeout_seconds` per attempt and
`budget_seconds` in all (both from the overlay: nothing here is a default). A reader that has not answered is abandoned
(its thread is a daemon and its late answer is dropped), so one slow source costs the run its own budget and nothing of
any other source's. The last good read of each source is kept in a snapshot store; an over-budget or failing source
serves that snapshot, marked stale with its age, while it is no older than the source's `max_stale_age_seconds`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, ValidationError

from omnimarket.nodes.node_work_state_projection_compute.models.enum_source_reading import (
    EnumSourceReading,
)
from omnimarket.nodes.node_work_state_projection_compute.models.enum_work_state_source import (
    EnumWorkStateSource,
)
from omnimarket.nodes.node_work_state_projection_compute.models.model_source_budget import (
    ModelSourceBudget,
    ModelSourceSnapshot,
    ModelSourceStatus,
)


class SourceUnreadableError(Exception):
    """The whole source could not be read; the message is the cause carried into the status and `unread`."""


@dataclass(frozen=True)
class SourceRead:
    """What one read of a source returned: its rows, and notes on parts of it that could not be read."""

    items: tuple[BaseModel, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceSpec:
    """One source: its name, how to read it and the row model its snapshot is restored into."""

    source: EnumWorkStateSource
    read: Callable[[], SourceRead]
    item_type: type[BaseModel]


class ProtocolSnapshotStore(Protocol):
    def load(self, source: EnumWorkStateSource) -> ModelSourceSnapshot | None: ...

    def save(self, snapshot: ModelSourceSnapshot) -> None: ...


@dataclass
class InMemorySnapshotStore:
    """The default store: snapshots live as long as the process."""

    snapshots: dict[EnumWorkStateSource, ModelSourceSnapshot] = field(
        default_factory=dict
    )

    def load(self, source: EnumWorkStateSource) -> ModelSourceSnapshot | None:
        return self.snapshots.get(source)

    def save(self, snapshot: ModelSourceSnapshot) -> None:
        self.snapshots[snapshot.source] = snapshot


class FileSnapshotStore:
    """The real-backend store: one JSON file per source under a directory, written atomically."""

    def __init__(self, directory: Path) -> None:
        self._dir = directory

    def _path(self, source: EnumWorkStateSource) -> Path:
        return self._dir / f"{source.value}.json"

    def load(self, source: EnumWorkStateSource) -> ModelSourceSnapshot | None:
        try:
            return ModelSourceSnapshot.model_validate_json(
                self._path(source).read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return None

    def save(self, snapshot: ModelSourceSnapshot) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        target = self._path(snapshot.source)
        tmp = target.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_text(snapshot.model_dump_json(), encoding="utf-8")
        tmp.replace(target)


@dataclass
class _Attempt:
    done: threading.Event = field(default_factory=threading.Event)
    result: SourceRead | None = None
    error: str = ""


def _attempt(reader: Callable[[], SourceRead], seconds: float) -> _Attempt:
    outcome = _Attempt()

    def work() -> None:
        try:
            outcome.result = reader()
        except SourceUnreadableError as exc:
            outcome.error = str(exc) or "unreadable"
        except Exception as exc:
            outcome.error = f"{type(exc).__name__}: {exc}"
        finally:
            outcome.done.set()

    threading.Thread(target=work, daemon=True).start()
    outcome.done.wait(seconds)
    return outcome


def read_source_budgeted(
    spec: SourceSpec,
    budget: ModelSourceBudget,
    store: ProtocolSnapshotStore,
    now: datetime,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[SourceRead, ModelSourceStatus]:
    """One source, read within its budget. Never raises and never waits longer than `budget.budget_seconds`."""
    started = clock()
    deadline = started + budget.budget_seconds
    cause = "no attempt made"
    for _ in range(budget.attempts):
        remaining = deadline - clock()
        if remaining <= 0:
            break
        outcome = _attempt(spec.read, min(budget.timeout_seconds, remaining))
        if outcome.done.is_set() and outcome.result is not None:
            store.save(
                ModelSourceSnapshot(
                    source=spec.source,
                    read_at=now,
                    items=tuple(
                        item.model_dump(mode="json") for item in outcome.result.items
                    ),
                )
            )
            return outcome.result, ModelSourceStatus(
                source=spec.source,
                reading=EnumSourceReading.FRESH,
                read_at=now,
                age_seconds=0.0,
                waited_seconds=clock() - started,
            )
        cause = (
            outcome.error
            if outcome.done.is_set()
            else f"timed out after {min(budget.timeout_seconds, remaining):g}s"
        )
    waited = clock() - started
    snapshot = store.load(spec.source)
    if snapshot is None:
        return SourceRead(), ModelSourceStatus(
            source=spec.source,
            reading=EnumSourceReading.UNREAD,
            waited_seconds=waited,
            cause=f"{cause}; no snapshot",
        )
    age = (now - snapshot.read_at).total_seconds()
    if age > budget.max_stale_age_seconds:
        return SourceRead(), ModelSourceStatus(
            source=spec.source,
            reading=EnumSourceReading.UNREAD,
            read_at=snapshot.read_at,
            age_seconds=age,
            waited_seconds=waited,
            cause=f"{cause}; snapshot {age:g}s old, older than {budget.max_stale_age_seconds:g}s",
        )
    try:
        items = tuple(spec.item_type.model_validate(d) for d in snapshot.items)
    except ValidationError:
        return SourceRead(), ModelSourceStatus(
            source=spec.source,
            reading=EnumSourceReading.UNREAD,
            waited_seconds=waited,
            cause=f"{cause}; snapshot does not validate",
        )
    return SourceRead(items=items), ModelSourceStatus(
        source=spec.source,
        reading=EnumSourceReading.STALE,
        read_at=snapshot.read_at,
        age_seconds=age,
        waited_seconds=waited,
        cause=cause,
    )


def read_sources_budgeted(
    specs: tuple[SourceSpec, ...],
    budgets: dict[EnumWorkStateSource, ModelSourceBudget],
    store: ProtocolSnapshotStore,
    now: datetime,
) -> dict[EnumWorkStateSource, tuple[SourceRead, ModelSourceStatus]]:
    """Every source on its own thread at once, so the run takes the longest single budget and never their sum."""
    results: dict[EnumWorkStateSource, tuple[SourceRead, ModelSourceStatus]] = {}

    def run(spec: SourceSpec) -> None:
        results[spec.source] = read_source_budgeted(
            spec, budgets[spec.source], store, now
        )

    threads = [threading.Thread(target=run, args=(s,), daemon=True) for s in specs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


__all__ = [
    "FileSnapshotStore",
    "InMemorySnapshotStore",
    "ProtocolSnapshotStore",
    "SourceRead",
    "SourceSpec",
    "SourceUnreadableError",
    "read_source_budgeted",
    "read_sources_budgeted",
]
