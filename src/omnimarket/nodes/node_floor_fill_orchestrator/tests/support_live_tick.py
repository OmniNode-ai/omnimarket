# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One live-profile tick on a recorded floor and a pool snapshot, for the tests that read what a real tick stores.

The tick runs ``run_entry`` over ``build_live_handler_factory`` with an in-memory store, the recorded floor made
current, a lane host pool snapshot written to disk and the pool clock injected, so the snapshot's age is fixed by the
test and never by the wall clock.
"""

from __future__ import annotations

import json
import sys
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omnibase_spi.contracts.services import (
    ModelLaneHostPoolReading,
    ModelLaneHostPoolSnapshot,
)

from omnimarket.nodes.node_floor_fill_orchestrator.container.container_live import (
    build_live_handler_factory,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_live_settings import (
    load_floor_fill_live_settings,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.scripted_ports import (
    ScriptedTickets,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.store import (
    InMemoryFloorStateStore,
)
from omnimarket.nodes.node_floor_fill_orchestrator.runtime_floor_fill_orchestrator import (
    run_entry,
)
from omnimarket.nodes.node_floor_fill_orchestrator.tests.support import (
    overlay,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.adapter_snapshot_file import (
    FileSnapshotStore,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.pool_snapshot_refusals import (
    ACCOUNTS_UNREAD_PREFIX,
)
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.models.model_pool_snapshot import (
    ModelPoolHostRow,
    ModelPoolSnapshotRecord,
)

RECORDED_FLOOR = (
    Path(__file__).resolve().parents[2]
    / "node_floor_fill_candidates_compute"
    / "tests"
    / "fixtures"
    / "factory-floor-2026-11-02T1040Z.json"
)
SUFFIX = ".read-at.json"
SCOPE = "floor-fill-test"
# The bound the live settings carry, and a different one the snapshot record declares for itself: the reader takes
# the settings' bound, so a test that moves the snapshot between the two reads which one governs.
SETTINGS_MAX_AGE_SECONDS = 180
RECORD_MAX_AGE_SECONDS = 60


def write_snapshot(
    path: Path,
    *,
    free: int,
    read_at: datetime,
    cap: int | None = None,
    away_slots: int = 0,
    unread_hosts: tuple[str, ...] = (),
    engines: tuple[str, ...] = ("claude",),
) -> None:
    """Host ``h1`` admitting ``free`` slots (its cap differs), an away host holding ``away_slots``, and the hosts in
    ``unread_hosts`` as the snapshot unit writes a host whose token pool read failed: zero slots and an
    ``accounts-unread`` refusal."""
    readings = [
        ModelLaneHostPoolReading(
            name="h1",
            lane_slots=free,
            lane_cap=free + 3 if cap is None else cap,
            placed=3,
            engines=engines,
        ),
        ModelLaneHostPoolReading(
            name="h2",
            lane_slots=away_slots,
            lane_cap=away_slots,
            lane_admission_refusal="away: parked",
            engines=engines,
        ),
    ]
    for host in unread_hosts:
        readings.append(
            ModelLaneHostPoolReading(
                name=host,
                lane_slots=0,
                lane_admission_refusal=f"{ACCOUNTS_UNREAD_PREFIX}: token pool command rc=3: no overlay roots",
                engines=(),
            )
        )
    snapshot = ModelLaneHostPoolSnapshot(readings=tuple(readings))
    record = ModelPoolSnapshotRecord(
        correlation_id=uuid.uuid4(),
        read_at=read_at,
        max_age_seconds=RECORD_MAX_AGE_SECONDS,
        hosts=(
            ModelPoolHostRow(
                host="h1",
                declared_slots=free + 3,
                running_lanes=3,
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


def live_section(tmp_path: Path, engine: str = "claude") -> dict[str, Any]:
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
        "pool_snapshot_max_age_seconds": SETTINGS_MAX_AGE_SECONDS,
        "pool_engine": engine,
    }


def recorded_floor(now: datetime) -> tuple[str, dict[str, list[str]]]:
    """The recorded floor of 2026-11-02 (the candidates node's fixture) made current: its text and each line's plan."""
    body = json.loads(RECORDED_FLOOR.read_text(encoding="utf-8"))
    body["generated_at"] = now.isoformat()
    return json.dumps(body), {line["line_id"]: [] for line in body["lines"]}


def live_tick(
    tmp_path: Path,
    *,
    free: int,
    age_seconds: int,
    engine: str = "claude",
    bind_seams: bool = True,
    **snapshot: Any,
) -> tuple[str, int, InMemoryFloorStateStore]:
    """Tick once; the snapshot was read ``age_seconds`` before the injected pool clock, which the tick reads."""
    now = datetime.now(UTC)
    write_snapshot(
        tmp_path / "pool.json",
        free=free,
        read_at=now - timedelta(seconds=age_seconds),
        **snapshot,
    )
    (tmp_path / "ledger.md").write_text("", encoding="utf-8")
    (tmp_path / "out").mkdir()
    record, plan_lines = recorded_floor(now)
    plans = tmp_path / "plans"
    plans.mkdir()
    for line_id in plan_lines:
        (plans / f"{line_id}.md").write_text(f"# {line_id}\n", encoding="utf-8")
    config = overlay(
        plans_root=str(plans),
        runner_command=[sys.executable, "-c", "pass", "{lane}", "{key}", "{ticket}"],
        process_working_directory=str(tmp_path),
        stopped_reports_root=str(tmp_path),
        stopped_brief_directory=str(tmp_path / "briefs"),
    )
    store = InMemoryFloorStateStore()
    settings = load_floor_fill_live_settings(live_section(tmp_path, engine))
    seams: dict[str, Any] = (
        {
            "tickets": ScriptedTickets(),
            "brake": lambda: False,
            "stop": lambda _task_class: None,
        }
        if bind_seams
        else {}
    )
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
            pool_clock=lambda: now,
            **seams,
        ),
    )
    return result.stdout, result.exit_code, store


__all__: list[str] = [
    "RECORD_MAX_AGE_SECONDS",
    "SCOPE",
    "SETTINGS_MAX_AGE_SECONDS",
    "live_section",
    "live_tick",
    "recorded_floor",
    "write_snapshot",
]
