# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam: the floor fill reads its free slots from the pool snapshot the writer wrote (OMN-23664).

The other side is the fill orchestrator (the fillorch lane's node_floor_fill_orchestrator and its ProtocolFloorPool seam, which was
unbound: UnboundFloorPool answered "unbound:pool"). SnapshotFloorPool is the live source for that seam. A fill tick over the written
snapshot starts as many lanes as the snapshot has free slots; a snapshot older than the overlay's bound makes the pass DEGRADED
naming the cause. Either side changing shape fails here: the pool's refusal type, the reading's slot fields, or the tick's use of
the free-slot count.

Golden: a snapshot with two free slots (one free host with four cores, an away host) lets the tick start exactly two lanes.
Error: a record past the bound ends the tick DEGRADED (exit 3), starts nothing and names ``pool-unread:pool-snapshot``; a missing
record ends the same way.
Failing control: a pool that answered zero for the stale record would end the tick OK with no starts and hide the stale snapshot.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from omnibase_spi.protocols.lane import ProtocolLaneHostPool

from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EnumFloorFillCommand,
    EnumFloorFillStatus,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    FloorFillUnreadableError,
    ProtocolFloorPool,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import (
    ScriptedPool,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import Harness, harness
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.adapter_snapshot_file import (
    FileSnapshotStore,
    SnapshotFloorPool,
    StampedSnapshotFileLaneHostPool,
)
from tests.nodes.node_lane_host_pool_snapshot_effect.pool_inputs import (
    T0,
    accounts,
    advertisement,
    handler,
    host_row,
    overlay,
    pool_request,
)

pytestmark = pytest.mark.unit

TICK = EnumFloorFillCommand.TICK


def _write(tmp_path: Path) -> Path:
    """Write the snapshot: h1 free (4 cores: 2 lane slots), h2 away; two free slots in all."""
    path = tmp_path / "pool.json"
    result = handler(
        ads=(advertisement("h1", cores=4),),
        rows=(host_row("h1"), host_row("h2", available=False)),
        accs=(accounts("h1"),),
        store=FileSnapshotStore(path, ".read-at.json"),
        overlay_=overlay(snapshot_path=str(path)),
    ).handle(pool_request())
    assert result.record is not None
    assert result.record.free_slots_total == 2
    return path


def _fill_pool(path: Path, *, age: int = 0) -> SnapshotFloorPool:
    return SnapshotFloorPool(
        StampedSnapshotFileLaneHostPool(
            path, ".read-at.json", 180, lambda: T0 + timedelta(seconds=age)
        ),
        "claude_sonnet",
    )


def _tick(
    pool: ProtocolFloorPool,
) -> tuple[Harness, EnumFloorFillStatus, int, list[str]]:
    h = harness(pool=pool)
    answer = h.orchestrator().run(TICK)
    return h, answer.status, answer.exit_code, list(answer.lines)


def test_the_pool_is_the_fills_free_slot_seam_and_the_stamped_pool_is_the_starts_pool(
    tmp_path: Path,
) -> None:
    pool = _fill_pool(_write(tmp_path))
    assert isinstance(pool, ProtocolFloorPool)
    assert isinstance(
        StampedSnapshotFileLaneHostPool(tmp_path / "p.json", ".r", 1, lambda: T0),
        ProtocolLaneHostPool,
    )


def test_a_fill_tick_starts_as_many_lanes_as_the_snapshot_has_free_slots(
    tmp_path: Path,
) -> None:
    pool = _fill_pool(_write(tmp_path))
    assert pool.free_slots() == 2
    h, status, code, _ = _tick(pool)
    assert status is EnumFloorFillStatus.OK
    assert code == 0
    assert len(h.starter.starts) == 2


def test_error_a_stale_snapshot_degrades_the_tick_naming_the_cause_and_starts_nothing(
    tmp_path: Path,
) -> None:
    path = _write(tmp_path)
    with pytest.raises(FloorFillUnreadableError, match="stale"):
        _fill_pool(path, age=181).free_slots()
    h, status, code, lines = _tick(_fill_pool(path, age=181))
    assert status is EnumFloorFillStatus.DEGRADED
    assert code == 3
    assert h.starter.starts == []
    assert any(
        "pool-unread:pool-snapshot" in line and "stale" in line for line in lines
    )


def test_error_a_missing_record_degrades_the_tick(tmp_path: Path) -> None:
    pool = _fill_pool(tmp_path / "absent.json")
    h, status, _, lines = _tick(pool)
    assert status is EnumFloorFillStatus.DEGRADED
    assert h.starter.starts == []
    assert any("pool-unread:pool-snapshot" in line for line in lines)


def test_failing_control_a_pool_that_answers_zero_for_a_stale_record_hides_it(
    tmp_path: Path,
) -> None:
    """Zero free slots ends the tick OK: the typed refusal is what tells the operator the snapshot is stale."""
    _, status, code, _ = _tick(ScriptedPool(0))
    assert status is EnumFloorFillStatus.OK
    assert code == 0
    _, stale_status, stale_code, _ = _tick(_fill_pool(_write(tmp_path), age=181))
    assert (stale_status, stale_code) != (status, code)
