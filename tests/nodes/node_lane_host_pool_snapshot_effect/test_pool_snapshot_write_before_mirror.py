# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The pool snapshot and its read-time record are written before the emission mirror is touched (OMN-26376).

The fill refuses a snapshot older than 180 s. The writer's pass is bounded by a 50 s run limit inside its 60 s interval. The chain's
start emission publishes on the bus (the emission mirror) before the handler writes: a mirror that stalls spends the pass's whole
run limit, the run is ended (exit 124) and no snapshot is written, so the file ages past the fill's bound (observed ages 207 s to
957 s). The write comes first and the mirror's emissions after it, so a stalled mirror can no longer hold back the snapshot.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from omnibase_spi.contracts.services import ModelLaneHostPoolSnapshot

from omnimarket.models.floor_17a import ModelNodeEmission
from omnimarket.nodes.node_lane_host_pool_snapshot_effect import (
    runtime_pool_snapshot as runtime,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.pool_snapshot_ports import (
    InMemoryHostAccountsSource,
    InMemoryHostAdvertisementSource,
    InMemoryHostTableSource,
    InMemoryPoolSnapshotPublisher,
    InMemoryPoolSnapshotStore,
    InMemoryRunningLanesSource,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.pool_snapshot_wiring import (
    PoolSnapshotPorts,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.models.model_pool_snapshot import (
    ModelPoolSnapshotRecord,
)
from tests.nodes.node_lane_host_pool_snapshot_effect.pool_inputs import (
    accounts,
    advertisement,
    clock,
    host_row,
    overlay,
)

pytestmark = pytest.mark.unit

STEPS: list[str] = []


class _RecordingStore(InMemoryPoolSnapshotStore):
    def write(
        self, snapshot: ModelLaneHostPoolSnapshot, record: ModelPoolSnapshotRecord
    ) -> None:
        STEPS.append("store-write")
        super().write(snapshot, record)


class _StalledMirrorSink:
    """A journal sink whose mirror never answers: it records that it was touched, then fails as a timed-out mirror does."""

    def __init__(self, _path: Path) -> None:
        pass

    def emit(self, event: ModelNodeEmission) -> None:
        STEPS.append(f"mirror-{event.phase}")
        raise TimeoutError("emission mirror stalled")

    def read(self, since: datetime) -> tuple[ModelNodeEmission, ...]:
        return ()


def test_the_snapshot_is_written_before_the_first_mirror_emission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    STEPS.clear()
    monkeypatch.setattr(runtime, "JsonlEmissionSink", _StalledMirrorSink)
    store = _RecordingStore()
    ports = PoolSnapshotPorts(
        advertisements=InMemoryHostAdvertisementSource((advertisement("h1"),)),
        host_table=InMemoryHostTableSource((host_row("h1"),)),
        lanes=InMemoryRunningLanesSource({"h1": 0}),
        accounts=InMemoryHostAccountsSource((accounts("h1"),)),
        store=store,
        publisher=InMemoryPoolSnapshotPublisher(),
    )
    ov = overlay(
        snapshot_path=str(tmp_path / "pool.json"),
        emission_journal=str(tmp_path / "emissions.jsonl"),
    )
    with pytest.raises(TimeoutError):
        runtime.run_once(ov, ports=ports, now=clock(), write=True)
    assert STEPS[0] == "store-write", STEPS
    assert STEPS.count("mirror-start") == 1
    assert store.snapshot is not None
    assert store.record is not None
