# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Regression coverage for OMN-13774: spool writes must survive a missing dir.

``BoundedEventQueue._ensure_spool_dir`` creates the spool directory once at
construction time, but if that directory is later removed (tmp cleanup,
``ONEX_STATE_DIR`` reset between sessions, GC of an idle runtime dir) every
subsequent ``_spool_event`` write raised ``FileNotFoundError`` and was
swallowed as a logged "Failed to write spool file" -- silently losing the
entire buffer on shutdown-drain ("Drained 0 events from memory to spool").

The fix re-creates the spool dir (``mkdir(parents=True, exist_ok=True)``)
immediately before every spool write, not just at ``__init__`` time.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.nodes.node_emit_daemon.event_queue import (
    BoundedEventQueue,
    ModelQueuedEvent,
)
from omnimarket.nodes.node_emit_daemon.models.model_durability import (
    EnumDurabilityTier,
)

pytestmark = pytest.mark.unit


def _event(event_id: str) -> ModelQueuedEvent:
    return ModelQueuedEvent(
        event_id=event_id,
        event_type="telemetry.example",
        topic="onex.evt.omnimarket.telemetry-example.v1",
        payload={"correlation_id": event_id},
        partition_key=event_id,
        queued_at=datetime.now(UTC),
        tier=EnumDurabilityTier.TELEMETRY,
    )


@pytest.mark.asyncio
async def test_spool_event_recreates_missing_spool_dir(tmp_path: Path) -> None:
    """Spooling after the spool dir was deleted post-init must succeed, not raise."""
    spool_dir = tmp_path / "event-spool"
    outbox_dir = tmp_path / "event-outbox"

    queue = BoundedEventQueue(
        max_memory_queue=0,  # force straight to spool
        max_spool_messages=10,
        spool_dir=spool_dir,
        outbox_dir=outbox_dir,
    )
    assert spool_dir.exists()

    # Simulate the dir vanishing after construction (tmp cleanup / state reset).
    shutil.rmtree(spool_dir)
    assert not spool_dir.exists()

    ok = await queue.enqueue(_event("evt-1"))

    assert ok is True
    assert spool_dir.exists()
    assert queue.spool_size() == 1


@pytest.mark.asyncio
async def test_drain_to_spool_persists_buffer_when_spool_dir_missing(
    tmp_path: Path,
) -> None:
    """drain_to_spool must not silently lose the in-memory buffer on shutdown."""
    spool_dir = tmp_path / "event-spool"
    outbox_dir = tmp_path / "event-outbox"

    queue = BoundedEventQueue(
        max_memory_queue=5,
        max_spool_messages=10,
        spool_dir=spool_dir,
        outbox_dir=outbox_dir,
    )

    for i in range(3):
        assert await queue.enqueue(_event(f"evt-{i}")) is True
    assert queue.memory_size() == 3

    # Directory disappears between events being queued and shutdown-drain.
    shutil.rmtree(spool_dir)
    assert not spool_dir.exists()

    drained = await queue.drain_to_spool()

    assert drained == 3
    assert queue.spool_size() == 3
    assert queue.memory_size() == 0
