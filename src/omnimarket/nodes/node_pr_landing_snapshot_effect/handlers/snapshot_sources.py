# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared sources of the landing snapshot step: the PR watcher's records, in memory or its state file.

The watcher keys a pull request ``repo#n``; the tick's facts key it ``owner/repo#n``. A record gives
the PR's GraphQL node id (``facts.node_id``), the head its CI record was read at (``ci.sha``) and its
red checks (``ci.red_facts`` rows: check, conclusion, workflow run id, completed at). The bus the
snapshot records go to is injected (ProtocolTickPublisher of the tick orchestrator); this module
holds no Kafka client.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol

from omnimarket.handlers.emission_sink import (
    InMemoryEmissionSink,
    JsonlEmissionSink,
    ProtocolEmissionSink,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_landing_snapshot import (
    ModelWatcherPrRead,
    ModelWatcherRedRun,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.models.model_snapshot_overlay import (
    OVERLAY_ID,
    EnumSnapshotBackend,
    ModelSnapshotOverlay,
)

_RED_FACT_FIELDS = 4
_REQUIRED_FIELDS: dict[EnumSnapshotBackend, tuple[str, ...]] = {
    EnumSnapshotBackend.INMEMORY: (),
    EnumSnapshotBackend.REDPANDA: (
        "watcher_state_path",
        "bootstrap_servers",
        "emission_journal",
    ),
}


class ProtocolWatcherSource(Protocol):
    """The watcher's reads of the named pull requests (``repo#n``); a key it holds no record of is absent.

    Raises when the source cannot be read at all, so the tick is not recorded with unread facts.
    """

    def read(self, keys: Sequence[str]) -> dict[str, ModelWatcherPrRead]: ...


def watcher_key(pr: str) -> str:
    """``owner/repo#n`` as the watcher keys it, ``repo#n``."""
    return pr.partition("/")[2]


def watcher_pr_read(record: Mapping[str, object]) -> ModelWatcherPrRead:
    """One watcher record as the snapshot reads it. Rows whose run id is not a number are skipped."""
    raw_facts = record.get("facts")
    raw_ci = record.get("ci")
    facts: Mapping[str, object] = raw_facts if isinstance(raw_facts, Mapping) else {}
    ci: Mapping[str, object] = raw_ci if isinstance(raw_ci, Mapping) else {}
    node_id = facts.get("node_id")
    head = ci.get("sha")
    runs: list[ModelWatcherRedRun] = []
    rows = ci.get("red_facts")
    for row in rows if isinstance(rows, list) else ():
        if not isinstance(row, list) or len(row) < _RED_FACT_FIELDS:
            continue
        check, conclusion, run_id = row[0], row[1], str(row[2])
        if not (check and conclusion and run_id.isdigit() and int(run_id) > 0):
            continue
        runs.append(
            ModelWatcherRedRun(
                check=str(check), conclusion=str(conclusion), run_id=int(run_id)
            )
        )
    return ModelWatcherPrRead(
        node_id=str(node_id) if node_id else None,
        ci_head=str(head) if head else None,
        red_runs=tuple(runs),
        read_at=str(ci.get("read_at") or ""),
    )


class InMemoryWatcherSource:
    """Watcher records given as the watcher writes them, keyed ``repo#n``."""

    def __init__(self, records: Mapping[str, Mapping[str, object]]) -> None:
        self._records = dict(records)

    def read(self, keys: Sequence[str]) -> dict[str, ModelWatcherPrRead]:
        return {
            key: watcher_pr_read(self._records[key])
            for key in keys
            if key in self._records
        }


class WatcherStateFileSource:
    """The PR watcher's state file, read once per tick so every PR of the tick comes from one read."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self, keys: Sequence[str]) -> dict[str, ModelWatcherPrRead]:
        state = json.loads(self._path.read_text(encoding="utf-8"))
        prs = state.get("prs") if isinstance(state, Mapping) else None
        if not isinstance(prs, Mapping):
            raise ValueError("the watcher state holds no prs map")
        return {
            key: watcher_pr_read(prs[key])
            for key in keys
            if isinstance(prs.get(key), Mapping)
        }


def load_snapshot_overlay(*, explicit: object | None = None) -> ModelSnapshotOverlay:
    """Read the overlay; a missing or invalid one refuses naming the fields it lacks."""
    try:
        overlay = load_structured_overlay(
            OVERLAY_ID, ModelSnapshotOverlay, explicit=explicit
        )
    except NodeOverlayError as exc:
        raise ValueError(str(exc)) from None
    missing = [
        name for name in _REQUIRED_FIELDS[overlay.backend] if not getattr(overlay, name)
    ]
    if missing:
        raise ValueError(
            f"backend {overlay.backend.value} needs overlay fields {missing}"
        )
    return overlay


def build_watcher_source(
    overlay: ModelSnapshotOverlay,
    *,
    inmemory: InMemoryWatcherSource | None = None,
) -> ProtocolWatcherSource:
    """The watcher source the overlay selects: injected records, or the watcher's state file."""
    if overlay.backend is EnumSnapshotBackend.INMEMORY:
        if inmemory is None:
            raise ValueError("backend inmemory needs the watcher records injected")
        return inmemory
    if overlay.watcher_state_path is None:
        raise ValueError("backend redpanda needs overlay field watcher_state_path")
    return WatcherStateFileSource(Path(overlay.watcher_state_path))


def build_emission(
    overlay: ModelSnapshotOverlay, *, journal_root: Path | None = None
) -> tuple[ProtocolEmissionSink, str]:
    """The floor emission sink the overlay selects (in memory, or the journal) and its backend label."""
    if overlay.backend is EnumSnapshotBackend.INMEMORY:
        return InMemoryEmissionSink(), "in_memory"
    if overlay.emission_journal is None:
        raise ValueError("backend redpanda needs overlay field emission_journal")
    path = Path(overlay.emission_journal)
    return JsonlEmissionSink(journal_root / path if journal_root else path), "real"


__all__: list[str] = [
    "InMemoryWatcherSource",
    "ProtocolWatcherSource",
    "WatcherStateFileSource",
    "build_emission",
    "build_watcher_source",
    "load_snapshot_overlay",
    "watcher_key",
    "watcher_pr_read",
]
