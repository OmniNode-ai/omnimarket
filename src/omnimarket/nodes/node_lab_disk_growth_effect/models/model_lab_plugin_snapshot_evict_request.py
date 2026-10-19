# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Snapshot retention policy for one lab host."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ModelLabPluginSnapshotEvictRequest:
    """Keep recent plugin commits while protecting running lanes."""

    plugins_dir: Path
    keep_newest: int = 3
    dry_run: bool = False
