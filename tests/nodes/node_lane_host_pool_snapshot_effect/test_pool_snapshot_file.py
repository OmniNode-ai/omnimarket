# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lane host pool snapshot file store and adapter (OMN-23664).

Golden: FileSnapshotStore writes the snapshot and record atomically; StampedSnapshotFileLaneHostPool
reads it back through the shared SnapshotFileLaneHostPool with an equal ModelLaneHostPoolSnapshot.
Error: a record older than max_age_seconds, a missing record, or a snapshot file with an extra key
is refused by the adapter.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest
from omnibase_spi.contracts.services import (
    ModelLaneHostPoolRequest,
    ModelLaneHostPoolSnapshot,
)
from pydantic import ValidationError

from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.adapter_snapshot_file import (
    FileSnapshotStore,
    StampedSnapshotFileLaneHostPool,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.handler_pool_snapshot import (
    HandlerPoolSnapshot,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.pool_snapshot_ports import (
    InMemoryHostAccountsSource,
    InMemoryHostAdvertisementSource,
    InMemoryHostTableSource,
    InMemoryPoolSnapshotPublisher,
    InMemoryRunningLanesSource,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.snapshot_errors import (
    PoolSnapshotStaleError,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.models.model_pool_snapshot import (
    EnumPoolSnapshotStatus,
)
from tests.nodes.node_lane_host_pool_snapshot_effect.pool_inputs import (
    T0,
    accounts,
    advertisement,
    clock,
    host_row,
    overlay,
    pool_request,
)

pytestmark = pytest.mark.unit


def _write_snapshot(tmp_path: Path) -> tuple[Path, Path, ModelLaneHostPoolSnapshot]:
    snap_path = tmp_path / "pool.json"
    record_path = tmp_path / "pool.json.read-at.json"
    store = FileSnapshotStore(snap_path, ".read-at.json")
    handler = HandlerPoolSnapshot(
        overlay=overlay(),
        advertisement_source=InMemoryHostAdvertisementSource((advertisement("h1"),)),
        host_table_source=InMemoryHostTableSource((host_row("h1"),)),
        lanes_source=InMemoryRunningLanesSource({}),
        accounts_source=InMemoryHostAccountsSource((accounts("h1"),)),
        store=store,
        publisher=InMemoryPoolSnapshotPublisher(),
        clock=clock(),
    )
    result = handler.handle(pool_request())
    assert result.status is EnumPoolSnapshotStatus.WRITTEN
    assert snap_path.exists()
    assert record_path.exists()
    snapshot = ModelLaneHostPoolSnapshot.model_validate_json(
        snap_path.read_text(encoding="utf-8")
    )
    return snap_path, record_path, snapshot


def test_file_snapshot_store_writes_both_files_atomically(tmp_path: Path) -> None:
    snap_path, record_path, _ = _write_snapshot(tmp_path)
    assert not list(tmp_path.glob("*.tmp"))
    assert "read_at" not in json.loads(snap_path.read_text(encoding="utf-8"))
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["read_at"].startswith("2026-11-11T03:00:00")
    assert FileSnapshotStore(snap_path, ".read-at.json").read_record() is not None


def test_stamped_snapshot_file_lane_host_pool_reads_back_equal_snapshot(
    tmp_path: Path,
) -> None:
    snap_path, _, expected_snapshot = _write_snapshot(tmp_path)
    pool = StampedSnapshotFileLaneHostPool(snap_path, ".read-at.json", 180, clock())
    snapshot = pool.read_pool(ModelLaneHostPoolRequest(engine="claude_opus"))
    assert snapshot == expected_snapshot


def test_stamped_snapshot_file_lane_host_pool_refuses_stale_record(
    tmp_path: Path,
) -> None:
    snap_path, _, _ = _write_snapshot(tmp_path)
    fresh = StampedSnapshotFileLaneHostPool(
        snap_path, ".read-at.json", 60, clock(T0 + timedelta(seconds=60))
    )
    fresh.read_pool(ModelLaneHostPoolRequest(engine="claude_opus"))
    stale = StampedSnapshotFileLaneHostPool(
        snap_path, ".read-at.json", 60, clock(T0 + timedelta(seconds=61))
    )
    with pytest.raises(PoolSnapshotStaleError, match="stale"):
        stale.read_pool(ModelLaneHostPoolRequest(engine="claude_opus"))


def test_stamped_snapshot_file_lane_host_pool_refuses_missing_record(
    tmp_path: Path,
) -> None:
    snap_path = tmp_path / "pool.json"
    pool = StampedSnapshotFileLaneHostPool(snap_path, ".read-at.json", 180, clock())
    with pytest.raises(PoolSnapshotStaleError):
        pool.read_pool(ModelLaneHostPoolRequest(engine="claude_opus"))


def test_stamped_snapshot_file_lane_host_pool_refuses_extra_key_in_snapshot(
    tmp_path: Path,
) -> None:
    snap_path, _, _ = _write_snapshot(tmp_path)
    data = json.loads(snap_path.read_text(encoding="utf-8"))
    data["read_at"] = T0.isoformat()  # the read time cannot live in the snapshot file
    snap_path.write_text(json.dumps(data), encoding="utf-8")
    pool = StampedSnapshotFileLaneHostPool(snap_path, ".read-at.json", 180, clock())
    with pytest.raises(ValidationError):
        pool.read_pool(ModelLaneHostPoolRequest(engine="claude_opus"))
