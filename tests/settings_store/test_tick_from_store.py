# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Task 22: the tick's overlay documents come from the injected store, and an unreachable, absent or stale overlay ends it degraded (75)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from omnimarket.handlers.overlay_store.store_in_memory import InMemoryOverlayStore
from omnimarket.handlers.overlay_store.tick_from_store import (
    EXIT_STORE_UNAVAILABLE,
    TickStoreUnavailableError,
    read_tick_overlays,
)

NOW = datetime(2026, 11, 24, 13, 0, tzinfo=UTC)
BOUND = 900.0
IDS = ("node_floor_fill_orchestrator", "lab_hosts")


class _Memory(InMemoryOverlayStore):
    async def write_version(
        self, record: Any
    ) -> bool:  # the plain store keeps what it is given
        self._records[str(record["overlay_id"])] = dict(record)
        return True


def _record(overlay_id: str, beat: datetime | None) -> dict[str, Any]:
    record: dict[str, Any] = {"overlay_id": overlay_id, "document": {"k": overlay_id}}
    if beat is not None:
        record["heartbeat_at"] = beat.isoformat()
    return record


async def _store(**beats: datetime | None) -> _Memory:
    store = _Memory()
    for overlay_id in IDS:
        await store.write_version(_record(overlay_id, beats.get(overlay_id, NOW)))
    return store


def test_tick_from_store_the_unavailable_exit_is_75() -> None:
    assert EXIT_STORE_UNAVAILABLE == 75


async def test_tick_from_store_reads_both_overlays_from_the_store() -> None:
    documents = await read_tick_overlays(
        await _store(), IDS, now=NOW, max_staleness_seconds=BOUND
    )
    assert documents == {i: {"k": i} for i in IDS}


async def test_tick_from_store_an_unreachable_store_is_unavailable() -> None:
    class _Closed(_Memory):
        async def read_current(self, overlay_id: str) -> Any:
            raise ConnectionRefusedError("closed port")

    with pytest.raises(
        TickStoreUnavailableError, match="unreachable:node_floor_fill_orchestrator"
    ):
        await read_tick_overlays(_Closed(), IDS, now=NOW, max_staleness_seconds=BOUND)


async def test_tick_from_store_an_absent_overlay_is_unavailable_and_reads_no_file() -> (
    None
):
    store = _Memory()
    await store.write_version(_record("node_floor_fill_orchestrator", NOW))
    with pytest.raises(TickStoreUnavailableError, match="absent:lab_hosts"):
        await read_tick_overlays(store, IDS, now=NOW, max_staleness_seconds=BOUND)


async def test_tick_from_store_a_heartbeat_past_the_bound_is_unavailable() -> None:
    store = await _store(lab_hosts=NOW - timedelta(seconds=BOUND + 1))
    with pytest.raises(TickStoreUnavailableError, match="stale:lab_hosts"):
        await read_tick_overlays(store, IDS, now=NOW, max_staleness_seconds=BOUND)


async def test_tick_from_store_a_record_with_no_heartbeat_is_unavailable() -> None:
    store = await _store(lab_hosts=None)
    with pytest.raises(TickStoreUnavailableError, match="stale:lab_hosts"):
        await read_tick_overlays(store, IDS, now=NOW, max_staleness_seconds=BOUND)
