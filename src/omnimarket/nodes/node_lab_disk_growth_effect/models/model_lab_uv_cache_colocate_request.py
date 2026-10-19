# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Filesystem and config targets for a hardlinkable uv cache."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ModelLabUvCacheColocateRequest:
    """Place the cache on the worktrees' filesystem."""

    worktrees_root: Path
    cache_dir: Path
    uv_config: Path
    dry_run: bool = False
