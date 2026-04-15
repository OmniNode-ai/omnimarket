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
import tempfile
from collections.abc import Generator
from contextlib import contextmanager, suppress
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
    lock_dir = os.path.dirname(path)
    os.makedirs(lock_dir, exist_ok=True)

    now = datetime.now(tz=UTC)
    payload = {
        "tick_id": tick_id,
        "acquired_at": now.isoformat(),
        "holder": holder,
    }
    payload_bytes = json.dumps(payload, indent=2).encode("utf-8")

    # Attempt atomic creation first (O_CREAT|O_EXCL guarantees no race).
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, payload_bytes)
        finally:
            os.close(fd)
        logger.info("Dispatch lease acquired: tick_id=%s holder=%s", tick_id, holder)
        return
    except FileExistsError:
        pass

    # File already exists — read it and decide whether it is stale.
    try:
        with open(path, encoding="utf-8") as fh:
            existing = json.load(fh)
        acquired_at = datetime.fromisoformat(existing["acquired_at"])
        age = now - acquired_at
        if age.total_seconds() < LEASE_EXPIRY_SECONDS:
            raise DispatchLeaseHeldError(existing["holder"], acquired_at)
        logger.warning(
            "Stale dispatch lease (age=%ds, holder=%s) — overwriting",
            int(age.total_seconds()),
            existing.get("holder", "unknown"),
        )
    except (json.JSONDecodeError, KeyError, ValueError):
        logger.warning("Corrupt dispatch-lock.json — overwriting")

    # Atomically replace the stale/corrupt lease file.
    fd, tmp_path = tempfile.mkstemp(dir=lock_dir, suffix=".tmp")
    try:
        os.write(fd, payload_bytes)
        os.close(fd)
        os.replace(tmp_path, path)
    except Exception:
        os.close(fd)
        with suppress(OSError):
            os.unlink(tmp_path)
        raise
    logger.info("Dispatch lease acquired: tick_id=%s holder=%s", tick_id, holder)


def release_dispatch_lease(state_dir: str, tick_id: str | None = None) -> None:
    """Release the file-based dispatch lease.

    Should be called in a finally block.  Failure to delete is non-fatal —
    the lease expires automatically after LEASE_EXPIRY_SECONDS.

    Args:
        state_dir: Path to the .onex_state directory.
        tick_id:   The tick_id used when acquiring the lease.  When provided,
                   the stored tick_id is verified before deletion so a newer
                   holder's lease is never removed.
    """
    path = _lock_path(state_dir)
    try:
        if not os.path.exists(path):
            return
        if tick_id is not None:
            try:
                with open(path, encoding="utf-8") as fh:
                    stored = json.load(fh)
                stored_tick = stored.get("tick_id")
                if stored_tick != tick_id:
                    logger.warning(
                        "release_dispatch_lease: stored tick_id=%r != caller tick_id=%r "
                        "— skipping deletion to protect newer holder's lease "
                        "(will expire in %ds)",
                        stored_tick,
                        tick_id,
                        LEASE_EXPIRY_SECONDS,
                    )
                    return
            except (json.JSONDecodeError, OSError):
                pass
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
        release_dispatch_lease(state_dir, tick_id=tick_id)


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
