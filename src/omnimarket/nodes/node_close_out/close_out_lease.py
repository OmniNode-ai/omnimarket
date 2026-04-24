# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""File-based concurrent-run guard for node_close_out.

Close-out orchestrates side-effecting work (merge-sweep, deploy, release) and
cannot safely overlap with another run of itself. A second invocation fired
while the first is still running (manual fire during scheduled fire, or a slow
cron tick overlapping the next tick) can double-merge PRs, double-tag releases,
or corrupt run state.

The guard is a PID-file-style lease: a small JSON document written to
``{state_dir}/close-out-lock.json`` at start and deleted in a finally block at
end. A lease older than ``LEASE_EXPIRY_SECONDS`` (default 2x the longest
expected close-out duration) is considered stale and reclaimable, so a crashed
prior run cannot wedge the pipeline indefinitely.

This mirrors the canonical pattern in
``omniclaude/scripts/cron-merge-sweep.sh`` (LOCK_FILE + age-based staleness)
and ``omnimarket/nodes/node_session_bootstrap/dispatch_lease.py`` (atomic
O_CREAT|O_EXCL acquire + tick-scoped release).
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

_LOCK_FILENAME = "close-out-lock.json"

CLOSE_OUT_TIMEOUT_SECONDS: int = 300
LEASE_EXPIRY_SECONDS: int = 2 * CLOSE_OUT_TIMEOUT_SECONDS


class CloseOutLeaseHeldError(Exception):
    """Raised when another close-out run is holding the lease (non-stale)."""

    def __init__(self, holder: str, acquired_at: datetime) -> None:
        self.holder = holder
        self.acquired_at = acquired_at
        super().__init__(
            f"Close-out lease held by '{holder}' since {acquired_at.isoformat()}"
        )


def _lock_path(state_dir: str) -> str:
    return os.path.join(os.path.abspath(state_dir), _LOCK_FILENAME)


def _now() -> datetime:
    return datetime.now(tz=UTC)


def acquire_close_out_lease(
    state_dir: str,
    correlation_id: str,
    holder: str,
    now: datetime | None = None,
) -> None:
    """Acquire the file-based close-out lease.

    Raises:
        CloseOutLeaseHeldError: If a non-stale lease already exists.
    """
    path = _lock_path(state_dir)
    lock_dir = os.path.dirname(path)
    os.makedirs(lock_dir, exist_ok=True)

    acquired_at = now or _now()
    payload = {
        "correlation_id": correlation_id,
        "acquired_at": acquired_at.isoformat(),
        "holder": holder,
        "pid": os.getpid(),
    }
    payload_bytes = json.dumps(payload, indent=2).encode("utf-8")

    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, payload_bytes)
        finally:
            os.close(fd)
        logger.info(
            "Close-out lease acquired: correlation_id=%s holder=%s",
            correlation_id,
            holder,
        )
        return
    except FileExistsError:
        pass

    try:
        with open(path, encoding="utf-8") as fh:
            existing = json.load(fh)
        existing_acquired_at = datetime.fromisoformat(existing["acquired_at"])
        age = acquired_at - existing_acquired_at
        if age.total_seconds() < LEASE_EXPIRY_SECONDS:
            raise CloseOutLeaseHeldError(existing["holder"], existing_acquired_at)
        logger.warning(
            "Stale close-out lease (age=%ds, holder=%s) — reclaiming",
            int(age.total_seconds()),
            existing.get("holder", "unknown"),
        )
    except (json.JSONDecodeError, KeyError, ValueError):
        logger.warning("Corrupt close-out-lock.json — reclaiming")

    fd, tmp_path = tempfile.mkstemp(dir=lock_dir, suffix=".tmp")
    try:
        os.write(fd, payload_bytes)
        os.close(fd)
        os.replace(tmp_path, path)
    except Exception:
        with suppress(OSError):
            os.close(fd)
        with suppress(OSError):
            os.unlink(tmp_path)
        raise
    logger.info(
        "Close-out lease acquired (reclaimed stale): correlation_id=%s holder=%s",
        correlation_id,
        holder,
    )


def release_close_out_lease(state_dir: str, correlation_id: str | None = None) -> None:
    """Release the close-out lease.

    Should be called in a finally block. When ``correlation_id`` is provided,
    the stored correlation_id is verified before deletion so a newer holder's
    lease is never removed. Failure to delete is non-fatal — the lease expires
    automatically after LEASE_EXPIRY_SECONDS.
    """
    path = _lock_path(state_dir)
    try:
        if not os.path.exists(path):
            return
        if correlation_id is not None:
            try:
                with open(path, encoding="utf-8") as fh:
                    stored = json.load(fh)
                stored_cid = stored.get("correlation_id")
                if stored_cid != correlation_id:
                    logger.warning(
                        "release_close_out_lease: stored correlation_id=%r != "
                        "caller correlation_id=%r — skipping deletion",
                        stored_cid,
                        correlation_id,
                    )
                    return
            except (json.JSONDecodeError, OSError):
                pass
        os.remove(path)
        logger.info("Close-out lease released: %s", path)
    except OSError as exc:
        logger.warning(
            "Failed to release close-out lease (will expire in %ds): %s",
            LEASE_EXPIRY_SECONDS,
            exc,
        )


def read_close_out_lease(state_dir: str) -> dict[str, object] | None:
    """Read current lease metadata without acquiring or releasing."""
    path = _lock_path(state_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return dict(json.load(fh))
    except (json.JSONDecodeError, OSError):
        return None


def lease_age(state_dir: str, now: datetime | None = None) -> timedelta | None:
    """Return age of the current lease, or None if no lease exists."""
    lease = read_close_out_lease(state_dir)
    if lease is None:
        return None
    try:
        acquired_at = datetime.fromisoformat(str(lease["acquired_at"]))
        return (now or _now()) - acquired_at
    except (KeyError, ValueError):
        return None


@contextmanager
def close_out_lease(
    state_dir: str,
    correlation_id: str,
    holder: str,
) -> Generator[None, None, None]:
    """Context manager that acquires and releases the close-out lease.

    Raises:
        CloseOutLeaseHeldError: If lease is already held (non-stale).
    """
    acquire_close_out_lease(state_dir, correlation_id, holder)
    try:
        yield
    finally:
        release_close_out_lease(state_dir, correlation_id=correlation_id)


__all__: list[str] = [
    "CLOSE_OUT_TIMEOUT_SECONDS",
    "LEASE_EXPIRY_SECONDS",
    "CloseOutLeaseHeldError",
    "acquire_close_out_lease",
    "close_out_lease",
    "lease_age",
    "read_close_out_lease",
    "release_close_out_lease",
]
