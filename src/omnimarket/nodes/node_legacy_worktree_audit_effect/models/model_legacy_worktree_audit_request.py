"""Input of the legacy worktree audit handler (OMN-20563)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ModelLegacyWorktreeAuditRequest:
    """The directory of worktrees to scan and the directory the two ledgers are written to."""

    root: Path
    out_dir: Path
