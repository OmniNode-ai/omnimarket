# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Input of the capability-catalog publish handler"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ModelCapabilityCatalogPublishRequest:
    """Where to collect from, where to keep state, and where and how to publish."""

    registry_root: Path
    onex_state_dir: Path
    kb_repo: Path
    branch: str
    sources_path: Path | None = None
    worktree: Path | None = None
    pr_state: Path | None = None
    dry_run: bool = False
    push: bool = True
