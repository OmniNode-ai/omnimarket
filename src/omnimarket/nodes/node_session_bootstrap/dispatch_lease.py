# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""File-based dispatch lease to prevent concurrent cron + triggered dispatches. (C4 fix)

Lease file: {state_dir}/dispatch-lock.json
Both build_dispatch_pulse and HandlerBuildLoopExecutor must acquire before dispatching.
30-minute expiry matches cron interval — any older lease is guaranteed stale.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Generator

logger = logging.getLogger(__name__)

_LEASE_EXPIRY_MINUTES = 30
_LEASE_FILENAME = "dispatch-lock.json"


def _lease_path(state_dir: str) -> str:
    return os.path.join(os.path.abspath(state_dir), _LEASE_FILENAME)


def try_acquire_lease(state_dir: str, tick_id: str, holder: str) -> bool:
    """Try to acquire the dispatch lease. Returns True if acquired, False if held."""
    path = _lease_path(state_dir)
    now = datetime.now(tz=UTC)

    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                existing = json.load(fh)
            acquired_at = datetime.fromisoformat(existing.get("acquired_at", "1970-01-01T00:00:00+00:00"))
            age = now - acquired_at
            if age < timedelta(minutes=_LEASE_EXPIRY_MINUTES):
                logger.info("dispatch lease held by %s (tick_id=%s, age=%s)", existing.get("holder"), existing.get("tick_id"), age)
                return False
            logger.warning("stale dispatch lease from %s (age=%s), overwriting", existing.get("holder"), age)
        except (json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("corrupt dispatch lease, overwriting: %s", exc)

    os.makedirs(os.path.dirname(path), exist_ok=True)
    lease = {"tick_id": tick_id, "acquired_at": now.isoformat(), "holder": holder}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(lease, fh, indent=2)
    logger.debug("dispatch lease acquired: tick_id=%s holder=%s", tick_id, holder)
    return True


def release_lease(state_dir: str) -> None:
    """Release the dispatch lease. Logs warning on failure — lease expires naturally."""
    path = _lease_path(state_dir)
    try:
        if os.path.exists(path):
            os.remove(path)
            logger.debug("dispatch lease released")
    except OSError as exc:
        logger.warning("failed to release dispatch lease (will expire in %d min): %s", _LEASE_EXPIRY_MINUTES, exc)


@contextmanager
def dispatch_lease(state_dir: str, tick_id: str, holder: str) -> Generator[bool, None, None]:
    """Context manager that acquires the lease and always releases in finally."""
    acquired = try_acquire_lease(state_dir, tick_id, holder)
    try:
        yield acquired
    finally:
        if acquired:
            release_lease(state_dir)


__all__: list[str] = ["dispatch_lease", "release_lease", "try_acquire_lease"]
