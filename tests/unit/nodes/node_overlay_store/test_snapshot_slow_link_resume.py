# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A reader whose link replays only part of the snapshot topic in one window still reaches the newest record (OMN-25529).

Recorded reading, host h105, 2026-11-17 to 2026-11-18: ``source last asserted 88162s ago, over the 21600s bound`` on
``lab_hosts``, then 95712 s, 108701 s and 100013 s on later ticks (the age went down and up, the asserted times were
2026-11-16T23:06Z, 21:06Z and 17:35Z), while the h053 loader published every hour and h053 itself read 1542 s. The topic held
5,309 records, every version of every overlay from the earliest retained offset; h105 took 2,534 of them in its 20 s
window, under a fresh consumer group each tick, so each tick answered with an old prefix of the log and wrote it over the
snapshot. The fake bus below delivers a fixed number of records per window from the group's own position, as a slow link does.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.models.overlay_store import (
    EnumOverlayReadState,
    ModelOverlayStoreReadOverlay,
    ModelOverlayStoreRecord,
    document_digest,
)
from omnimarket.nodes.node_overlay_store_read_effect import (
    runtime_overlay_store_read as read,
)
from tests.unit.nodes.node_overlay_store.support import (
    TABLE,
    Store,
    reader,
)

pytestmark = pytest.mark.unit

VERSIONS = 10
PER_WINDOW = 4
FIRST = datetime.now(UTC) - timedelta(hours=1)


def _version(n: int) -> ModelOverlayStoreRecord:
    table = {**TABLE, "bus_lane": f"lane-{n}"}
    return ModelOverlayStoreRecord(
        overlay_id="lab_hosts",
        digest=document_digest(table),
        document=table,
        source_ref=f"lab_hosts@v{n}",
        loaded_by="test",
        loaded_at=FIRST + timedelta(minutes=n),
    )


class SlowLinkBus:
    """A log of versions, a consumer group's committed position, and a window that ends after ``per_window`` records."""

    def __init__(self) -> None:
        self.log = [_version(n).model_dump_json().encode() for n in range(VERSIONS)]
        self.positions: dict[str, int] = {}
        self.groups: list[str] = []

    async def publish(
        self, topic: str, key: bytes | None, value: bytes, headers: Any = None
    ) -> None:
        return None

    async def subscribe(
        self,
        topic: str,
        on_message: Callable[[Any], Awaitable[None]],
        group_id: str,
        auto_offset_reset: str = "latest",
    ) -> Callable[[], Awaitable[None]]:
        self.groups.append(group_id)
        start = self.positions.get(group_id, 0)
        taken = self.log[start : start + PER_WINDOW]

        class Message:
            def __init__(self, value: bytes) -> None:
                self.value = value

        for value in taken:
            await on_message(Message(value))
        committed = start + len(taken)

        async def stop() -> None:
            self.positions[group_id] = committed

        return stop


def _tick(
    monkeypatch: pytest.MonkeyPatch, bus: SlowLinkBus, snapshot_root: Path
) -> Any:
    @contextlib.asynccontextmanager
    async def opened() -> AsyncIterator[SlowLinkBus]:
        yield bus

    monkeypatch.setattr(read, "open_lane_bus", lambda **_: opened())
    overlay = ModelOverlayStoreReadOverlay.model_validate(
        {
            "backend": "bus_snapshot",
            "bus_lane": "store-lane",
            "snapshot_root": str(snapshot_root),
            "read_window_s": 0.05,
            "host_name_env": "HOST",
            "overlays": {"lab_hosts": {"stale_after_s": 21600, "on_stale": "read"}},
        }
    )
    handler = read.build_handler(overlay, {"HOST": "h105"})
    return asyncio.run(handler.refresh_all())[0]


def test_each_tick_on_a_link_that_replays_part_of_the_log_gets_closer_to_the_newest_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bus = SlowLinkBus()
    newest = _version(VERSIONS - 1)
    readings = [_tick(monkeypatch, bus, tmp_path / "snap") for _ in range(5)]
    assert readings[0].digest == _version(PER_WINDOW - 1).digest
    assert readings[-1].digest == newest.digest, [r.digest for r in readings]
    held = [
        next(n for n in range(VERSIONS) if _version(n).digest == r.digest)
        for r in readings
    ]
    assert held == sorted(held), held


def test_a_window_with_nothing_new_leaves_the_snapshot_and_reports_no_store_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bus = SlowLinkBus()
    for _ in range(5):
        _tick(monkeypatch, bus, tmp_path / "snap")
    caught_up = _tick(monkeypatch, bus, tmp_path / "snap")
    assert caught_up.digest == _version(VERSIONS - 1).digest
    assert caught_up.state is EnumOverlayReadState.FRESH
    assert caught_up.store_error is None


def test_a_host_with_no_snapshot_listens_under_a_fresh_group_and_a_host_with_one_under_the_stable_group(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    bus = SlowLinkBus()
    _tick(monkeypatch, bus, tmp_path / "snap")
    _tick(monkeypatch, bus, tmp_path / "snap")
    assert not bus.groups[0].endswith(".snapshot"), bus.groups
    assert bus.groups[1] == "node_overlay_store_read_effect.h105.snapshot", bus.groups


def test_an_older_version_never_replaces_a_newer_snapshot(tmp_path: Path) -> None:
    newer = _version(8)
    older = _version(2)
    asyncio.run(
        reader(tmp_path, Store(newer.model_dump(mode="json"))).handle("lab_hosts")
    )
    reading = asyncio.run(
        reader(tmp_path, Store(older.model_dump(mode="json"))).handle("lab_hosts")
    )
    assert reading.digest == newer.digest
