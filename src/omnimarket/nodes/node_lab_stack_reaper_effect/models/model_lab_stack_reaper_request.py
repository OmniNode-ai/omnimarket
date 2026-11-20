# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One lease registration or one reaper pass."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


def default_lease_dir() -> Path:
    """The operator provisions one directory per docker daemon."""
    configured = os.environ.get("ONEX_LAB_STACK_LEASE_DIR", "").strip()
    return (
        Path(configured)
        if configured
        else Path.home() / ".omninode" / "lab-stack-leases"
    )


@dataclass(frozen=True, slots=True)
class ModelLabStackReaperRequest:
    verb: str
    lease_dir: Path = field(default_factory=default_lease_dir)
    ledger: Path | None = None
    project: str = ""
    lane: str = ""
    claim: str = ""
    generation: str = ""
    compose_files: tuple[Path, ...] = ()
    execute: bool = False
    docker_host: str | None = None
    retries: int = 3
    max_ledger_age_s: float = 900
    interval_min: float = 20
    cap_min: float = 10
    deadline_min: float = 30
    fence_wait_s: float = 30
    census: Path | None = None
    registry_root: Path | None = None
    now: datetime | None = None
    ledger_snapshot_command: tuple[str, ...] = ()
    harnesses: bool = False
    worktree_roots: tuple[Path, ...] = ()
    ticket_lane_entries: tuple[str, ...] | None = None
    ticket_inventory_required: bool = False
    ticket: str = "none"
