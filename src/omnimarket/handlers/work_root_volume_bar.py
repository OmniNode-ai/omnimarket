# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The volume bar a lane work root must stay under: refuse a root on a volume above 85 percent used (OMN-26371).

A host's lane work root holds every lane's worktree, venv and cache, so a root on a nearly full volume fails lanes
that the host's capacity bars would have admitted. The reading is the live volume the root sits on (``statvfs`` of
the root, or of its nearest existing parent when the root is not created yet), never a recorded figure.
``used percent`` is ``df``'s: used bytes over used plus the bytes available to an unprivileged user.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

VOLUME_BAR_PERCENT = 85.0


class ModelVolumeReading(BaseModel):
    """One live reading of the volume a path sits on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    used_bytes: int = Field(ge=0)
    available_bytes: int = Field(ge=0)

    @property
    def used_percent(self) -> float:
        total = self.used_bytes + self.available_bytes
        return 100.0 * self.used_bytes / total if total else 100.0


VolumeReader = Callable[[Path], ModelVolumeReading]


class WorkRootVolumeError(Exception):
    """The work root's volume is above the bar, or could not be read."""


def read_live_volume(path: Path) -> ModelVolumeReading:
    """Read the volume under ``path``, walking up to the nearest existing directory."""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    stat = os.statvfs(probe)
    return ModelVolumeReading(
        path=str(path),
        used_bytes=(stat.f_blocks - stat.f_bfree) * stat.f_frsize,
        available_bytes=stat.f_bavail * stat.f_frsize,
    )


def refuse_work_root_over_bar(
    root: Path,
    *,
    bar_percent: float = VOLUME_BAR_PERCENT,
    reader: VolumeReader = read_live_volume,
) -> ModelVolumeReading:
    """Return the reading when ``root``'s volume is at or below ``bar_percent`` used; raise otherwise."""
    try:
        reading = reader(root)
    except OSError as exc:
        raise WorkRootVolumeError(
            f"work root {root} sits on a volume that could not be read: {exc}"
        ) from exc
    if reading.used_percent > bar_percent:
        raise WorkRootVolumeError(
            f"work root {root} sits on a volume {reading.used_percent:.1f} percent used, above the {bar_percent:g} percent bar"
        )
    return reading
