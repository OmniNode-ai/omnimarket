# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The measured cause: the live profile read no pool, so a tick on a floor with free slots sent nothing.

Read on the fill host on 2026-11-16: the live profile bound the pool seam to ``UnboundFloorPool``, so every live tick
answered ``UNKNOWN pool-unread:unbound:pool`` and started no lane, while a pool snapshot that the lane host pool
snapshot unit writes every minute sat beside it. The live profile now reads that snapshot (the ``floor_fill_live``
``pool_snapshot_*`` keys): the same tick on the same recorded floor, with free slots in the snapshot, starts a lane.

Golden: a fresh snapshot with two free slots and a floor with ready tasks starts two lanes, each held RESERVED under its
``floor:<line_id>:<task_id>`` key until its CLAIM is read.
Error: a snapshot whose record is older than the bound starts nothing and names ``pool-unread:pool-snapshot``.
Failing control: a snapshot that reads zero free slots starts nothing and is not read as unbound.
"""

from __future__ import annotations

import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from omnibase_spi.contracts.services import (
    ModelLaneHostPoolReading,
    ModelLaneHostPoolSnapshot,
)

from omnimarket.nodes.node_floor_fill_orchestrator.container.container_live import (
    build_live_handler_factory,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EXIT_CODE_BY_STATUS,
    EnumFloorFillStatus,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_live_settings import (
    load_floor_fill_live_settings,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import (
    ScriptedTickets,
    floor_record,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.store import (
    InMemoryFloorStateStore,
)
from omnimarket.nodes.node_floor_fill_orchestrator.runtime_floor_fill_orchestrator import (
    run_entry,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import (
    LINES4,
    overlay,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.adapter_snapshot_file import (
    FileSnapshotStore,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.models.model_pool_snapshot import (
    ModelPoolHostRow,
    ModelPoolSnapshotRecord,
)

pytestmark = pytest.mark.unit

SUFFIX = ".read-at.json"
SCOPE = "floor-fill-test"


def _write_snapshot(path: Path, *, free: int, read_at: datetime) -> None:
    """One admitted host with ``free`` lane slots and one away host, as the snapshot unit writes them."""
    snapshot = ModelLaneHostPoolSnapshot(
        readings=(
            ModelLaneHostPoolReading(name="h1", lane_slots=free, lane_cap=free),
            ModelLaneHostPoolReading(
                name="h2", lane_slots=0, lane_admission_refusal="away: parked"
            ),
        )
    )
    record = ModelPoolSnapshotRecord(
        correlation_id=uuid.uuid4(),
        read_at=read_at,
        max_age_seconds=180,
        hosts=(
            ModelPoolHostRow(
                host="h1",
                declared_slots=free,
                running_lanes=0,
                free_slots=free,
                accounts_usable=("acct",),
                accounts_limited=(),
                away=False,
            ),
        ),
        free_slots_total=free,
        snapshot_path=str(path),
        provenance="test",
    )
    FileSnapshotStore(path, SUFFIX).write(snapshot, record)


def _live_section(tmp_path: Path) -> dict[str, Any]:
    return {
        "store_dsn_env": "EXAMPLE_FLOOR_FILL_DSN",
        "store_schema_name": "public",
        "store_table_name": "floor_fill_state",
        "store_connect_timeout_seconds": 5,
        "floor_fetch_timeout_seconds": 5.0,
        "plan_file_pattern": "{line_id}.md",
        "ledger_path": str(tmp_path / "ledger.md"),
        "process_output_dir": str(tmp_path / "out"),
        "pool_snapshot_path": str(tmp_path / "pool.json"),
        "pool_record_suffix": SUFFIX,
        "pool_snapshot_max_age_seconds": 180,
        "pool_engine": "claude",
    }


def _tick(
    tmp_path: Path, *, free: int, age_seconds: int
) -> tuple[str, int, InMemoryFloorStateStore]:
    now = datetime.now(UTC)
    _write_snapshot(
        tmp_path / "pool.json", free=free, read_at=now - timedelta(seconds=age_seconds)
    )
    (tmp_path / "ledger.md").write_text("", encoding="utf-8")
    (tmp_path / "out").mkdir()
    plans = tmp_path / "plans"
    plans.mkdir()
    for line_id in LINES4:
        (plans / f"{line_id}.md").write_text(f"# {line_id}\n", encoding="utf-8")
    record = floor_record(LINES4, generated_at=now)
    config = overlay(
        plans_root=str(plans),
        runner_command=[sys.executable, "-c", "pass", "{lane}", "{key}", "{ticket}"],
        process_working_directory=str(tmp_path),
        stopped_reports_root=str(tmp_path),
        stopped_brief_directory=str(tmp_path / "briefs"),
    )
    store = InMemoryFloorStateStore()
    settings = load_floor_fill_live_settings(_live_section(tmp_path))
    result = run_entry(
        ["tick"],
        {"floor_fill": config},
        host_id=config["host_id"],
        hosts=[(config["host_id"], True)],
        build_handler=build_live_handler_factory(
            settings,
            dsn="unused",
            store=store,
            fetch_text=lambda _url, _timeout: record,
            tickets=ScriptedTickets(),
            brake=lambda: False,
            stop=lambda _task_class: None,
        ),
    )
    return result.stdout, result.exit_code, store


def test_measured_cause_sends_a_lane_from_the_pool_snapshot(tmp_path: Path) -> None:
    out, code, store = _tick(tmp_path, free=2, age_seconds=5)
    assert "unbound:pool" not in out
    assert "pool-unread" not in out
    assert code == EXIT_CODE_BY_STATUS[EnumFloorFillStatus.OK], out
    row = store.load(SCOPE)
    assert row.version > 0
    assert row.payload is not None
    # A started lane is held RESERVED under its floor key until its CLAIM row is read; two free slots, two starts.
    assert row.payload.count('"state":"RESERVED"') == 2, row.payload
    assert '"key":"floor:alpha:a1"' in row.payload


def test_measured_cause_sends_nothing_from_a_stale_snapshot(tmp_path: Path) -> None:
    out, code, store = _tick(tmp_path, free=2, age_seconds=600)
    assert "pool-unread:pool-snapshot" in out
    assert code == EXIT_CODE_BY_STATUS[EnumFloorFillStatus.DEGRADED]
    assert store.load(SCOPE).version == 0


def test_measured_cause_sends_nothing_when_the_snapshot_has_no_free_slot(
    tmp_path: Path,
) -> None:
    out, _code, store = _tick(tmp_path, free=0, age_seconds=5)
    assert "unbound:pool" not in out
    assert "pool-unread" not in out
    row = store.load(SCOPE)
    assert row.payload is None or '"RESERVED"' not in row.payload
