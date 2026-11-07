# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dated history of the floor's numbers, and the read of one number at an earlier time (OMN-23018, AC1).

The history is an append-only JSON-lines file of snapshots. The read at ``target`` takes the nearest snapshot that holds
the key inside the window's tolerance; with none, the reading is unread with the reason, never zero.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from omnimarket.nodes.node_floor_compare_effect.models.model_floor_compare import (
    ModelReading,
    ModelSnapshot,
)


def read_history(path: Path) -> tuple[tuple[ModelSnapshot, ...], int]:
    """The snapshots in time order and the count of lines that did not parse (skipped, never repaired)."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return (), 0
    snapshots: list[ModelSnapshot] = []
    skipped = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            snapshots.append(ModelSnapshot.model_validate(json.loads(line)))
        except (ValueError, ValidationError):
            skipped += 1
    return tuple(sorted(snapshots, key=lambda s: s.at)), skipped


def append_snapshot(path: Path, snapshot: ModelSnapshot) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(snapshot.model_dump_json() + "\n")


def rewrite_history(path: Path, snapshots: tuple[ModelSnapshot, ...]) -> None:
    """Replace the file with ``snapshots`` through a temporary name in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        "".join(s.model_dump_json() + "\n" for s in snapshots), encoding="utf-8"
    )
    tmp.replace(path)


def prune(
    snapshots: tuple[ModelSnapshot, ...], now: datetime, retention_days: int
) -> tuple[ModelSnapshot, ...]:
    cutoff = now - timedelta(days=retention_days)
    return tuple(s for s in snapshots if s.at >= cutoff)


def value_in(snapshot: ModelSnapshot, key: str) -> float | None:
    """The value of ``key`` in one snapshot: the number it holds, zero when the snapshot lists the key's prefix in full and
    the key is not in it, else ``None`` (not read at that moment)."""
    if key in snapshot.values:
        return snapshot.values[key]
    if any(key.startswith(prefix) for prefix in snapshot.complete):
        return 0.0
    return None


def reading_at(
    snapshots: tuple[ModelSnapshot, ...],
    key: str,
    target: datetime,
    tolerance_hours: float,
) -> ModelReading:
    """The value of ``key`` at ``target``: the closest snapshot holding it within the tolerance, else unread."""
    tolerance = timedelta(hours=tolerance_hours)
    near = [s for s in snapshots if abs(s.at - target) <= tolerance]
    holding = [s for s in near if value_in(s, key) is not None]
    if holding:
        best = min(holding, key=lambda s: abs(s.at - target))
        return ModelReading(key=key, read=True, value=value_in(best, key), at=best.at)
    reason = (
        f"{len(near)} snapshot(s) within {tolerance_hours:g} h of {target:%Y-%m-%d %H:%MZ} and none read it"
        if near
        else f"no snapshot within {tolerance_hours:g} h of {target:%Y-%m-%d %H:%MZ}"
    )
    return ModelReading(key=key, read=False, reason=reason)
