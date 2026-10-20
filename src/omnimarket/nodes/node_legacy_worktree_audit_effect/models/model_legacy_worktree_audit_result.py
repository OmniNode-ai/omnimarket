"""Output of the legacy worktree audit handler (OMN-20563)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ModelLegacyWorktreeAuditResult:
    """Where the ledgers were written and how many worktrees fell into each class."""

    ledger_json: Path
    ledger_md: Path
    total: int
    by_class_counts: dict[str, int]
