"""Typed input and output of the cause-claim ownership rule (OMN-19998).

Frozen dataclasses, not pydantic: queue_status.py and throughput_tick.py, which call the handler, run in the offline
skill suites and on installed plugin copies, where only the standard library is installed.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ModelCauseClaimOwnershipRequest:
    """The throughput tick's result so far, the watcher state it read, and where the claims and workflows are read."""

    result: dict[str, Any]
    watcher_state: dict[str, Any] | None
    now: datetime
    ledger: Path | None
    registry_root: str
    queue_status: (
        Path  # queue-status's queue_status.py: the one reader of who owns a shared red
    )
    report_type: Any  # loops_check.Report: the status line's one formatter
    # OMN-17427: a near spelling of an unowned red on a live cause CLAIM goes to the exception queue only when ``emit``
    # (a read-only tick emits nothing); ``exception_dir`` overrides the queue's state directory, ``runner`` and
    # ``parent`` are the mechanize dispatch's
    emit: bool = False
    exception_dir: Path | None = None
    runner: Path | None = None
    parent: str | None = None
    plugin_repo: str | None = (
        None  # the repository the exception rows are filed against (the repos overlay's plugin_source)
    )


@dataclass(frozen=True)
class ModelCauseClaimOwnershipResult:
    """The tick's result with every owned shared_red_unowned line a NOTE (the same object when none changed)."""

    result: dict[str, Any]


@dataclass(frozen=True)
class ModelProcessFixOwnershipRequest:
    """The process-fix findings the tick built, and where the controller's leases and the ledger's CLAIMs are read."""

    found: list[
        tuple[str, str, str, str]
    ]  # loops_check.process_fix_findings: (level, loop, why, fix)
    landing: Path  # the landing controller's state directory (state.json)
    ledger: Path | None
    now: datetime
    queue_status: Path  # queue-status's queue_status.py: the owner readers
    watcher_state: dict[str, Any] | None = None
    registry_root: Path | None = None


@dataclass(frozen=True)
class ModelProcessFixOwnershipResult:
    """The findings with each owned PR's driver-lane MISSING turned into a NOTE naming its owners."""

    found: list[tuple[str, str, str, str]]
