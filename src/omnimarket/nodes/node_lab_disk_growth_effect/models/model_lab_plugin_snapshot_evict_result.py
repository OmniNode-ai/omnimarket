# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Snapshot eviction evidence for reports and status rows."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelLabPluginSnapshotEvictResult:
    """Only successfully removed trees contribute to reclaimed bytes."""

    status: str
    kept: tuple[str, ...] = ()
    kept_in_use: tuple[str, ...] = ()
    evicted: tuple[str, ...] = ()
    freed_bytes: int = 0
    reason: str = ""
    errors: tuple[str, ...] = ()

    def line(self) -> str:
        return (
            f"plugin_snapshots={self.status} evicted={len(self.evicted)} "
            f"freed={self.freed_bytes / 1e9:.1f}GB kept_in_use={len(self.kept_in_use)}"
        )
