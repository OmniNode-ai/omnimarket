# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""File-based dispatch lease for session bootstrap Rev 7.

Prevents the cron pulse (build_dispatch_pulse) and the triggered build loop
(HandlerBuildLoopExecutor) from dispatching simultaneously and creating
duplicate work (C4 fix from hostile review).

Lease file: {state_dir}/dispatch-lock.json
Schema:
  { "tick_id": "tick-20260412-0315",
    "acquired_at": "2026-04-12T03:15:00Z",
    "holder": "build_dispatch_pulse" }

Both dispatch paths must call acquire_dispatch_lease() before dispatching and
release_dispatch_lease() in a finally block.  A lease older than LEASE_EXPIRY_SECONDS
is considered stale and may be overwritten.

File mutex is sufficient: both paths run on the same machine in the same
Claude Code session.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

# Matches build_dispatch_pulse interval — any older lease is guaranteed stale.
LEASE_EXPIRY_SECONDS: int = 30 * 60  # 30 minutes

_LOCK_FILENAME = "dispatch-lock.json"


class DispatchLeaseHeldError(Exception):
    """Raised when the dispatch lease is held by another process."""

    def __init__(self, holder: str, acquired_at: datetime) -> None:
        self.holder = holder
        self.acquired_at = acquired_at
        super().__init__(
            f"Dispatch lease held by '{holder}' since {acquired_at.isoformat()}"
        )


# Backward-compatible alias
DispatchLeaseHeld = DispatchLeaseHeldError


def _lock_path(state_dir: str) -> str:
    return os.path.join(os.path.abspath(state_dir), _LOCK_FILENAME)


def acquire_dispatch_lease(
    state_dir: str,
    tick_id: str,
    holder: str,
) -> None:
    """Acquire the file-based dispatch lease.

    Args:
        state_dir: Path to the .onex_state directory.
        tick_id:   Unique identifier for this dispatch tick.
        holder:    Name of the acquiring process (e.g. 'build_dispatch_pulse').

    Raises:
        DispatchLeaseHeld: If a non-stale lease already exists.
    """
    path = _lock_path(state_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)

    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                existing = json.load(fh)
            acquired_at = datetime.fromisoformat(existing["acquired_at"])
            age = datetime.now(tz=UTC) - acquired_at
            if age.total_seconds() < LEASE_EXPIRY_SECONDS:
                raise DispatchLeaseHeldError(existing["holder"], acquired_at)
            logger.warning(
                "Stale dispatch lease (age=%ds, holder=%s) — overwriting",
                int(age.total_seconds()),
                existing.get("holder", "unknown"),
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            logger.warning("Corrupt dispatch-lock.json — overwriting")

    now = datetime.now(tz=UTC)
    payload = {
        "tick_id": tick_id,
        "acquired_at": now.isoformat(),
        "holder": holder,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    logger.info("Dispatch lease acquired: tick_id=%s holder=%s", tick_id, holder)


def release_dispatch_lease(state_dir: str) -> None:
    """Release the file-based dispatch lease.

    Should be called in a finally block.  Failure to delete is non-fatal —
    the lease expires automatically after LEASE_EXPIRY_SECONDS.
    """
    path = _lock_path(state_dir)
    try:
        if os.path.exists(path):
            os.remove(path)
            logger.info("Dispatch lease released: %s", path)
    except OSError as exc:
        logger.warning(
            "Failed to release dispatch lease (will expire in %ds): %s",
            LEASE_EXPIRY_SECONDS,
            exc,
        )


def read_dispatch_lease(state_dir: str) -> dict[str, str] | None:
    """Read current lease metadata without acquiring or releasing.

    Returns None if no lease exists or file is corrupt.
    """
    path = _lock_path(state_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return dict(json.load(fh))
    except (json.JSONDecodeError, OSError):
        return None


@contextmanager
def dispatch_lease(
    state_dir: str,
    tick_id: str,
    holder: str,
) -> Generator[None, None, None]:
    """Context manager that acquires and releases the dispatch lease.

    Usage:
        with dispatch_lease(state_dir, tick_id, "build_dispatch_pulse"):
            ... dispatch work ...

    Raises:
        DispatchLeaseHeld: If lease is already held (non-stale).
    """
    acquire_dispatch_lease(state_dir, tick_id, holder)
    try:
        yield
    finally:
        release_dispatch_lease(state_dir)


def make_tick_id(now: datetime | None = None) -> str:
    """Generate a deterministic tick ID from a timestamp.

    Format: tick-YYYYMMDD-HHMM
    """
    ts = now or datetime.now(tz=UTC)
    return ts.strftime("tick-%Y%m%d-%H%M")


def lease_age(state_dir: str) -> timedelta | None:
    """Return age of the current lease, or None if no lease exists."""
    lease = read_dispatch_lease(state_dir)
    if lease is None:
        return None
    try:
        acquired_at = datetime.fromisoformat(lease["acquired_at"])
        return datetime.now(tz=UTC) - acquired_at
    except (KeyError, ValueError):
        return None


__all__: list[str] = [
    "LEASE_EXPIRY_SECONDS",
    "DispatchLeaseHeld",
    "DispatchLeaseHeldError",
    "acquire_dispatch_lease",
    "dispatch_lease",
    "lease_age",
    "make_tick_id",
    "read_dispatch_lease",
    "release_dispatch_lease",
]
