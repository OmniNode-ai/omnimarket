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

import json
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
from omnimarket.nodes.node_floor_fill_orchestrator.models.model_floor_fill_config import (
    FloorFillConfigError,
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
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.models.model_pool_snapshot import (
    ModelPoolHostRow,
    ModelPoolSnapshotRecord,
)

pytestmark = pytest.mark.unit

RECORDED_FLOOR = (
    Path(__file__).resolve().parents[2]
    / "node_floor_fill_candidates_compute"
    / "tests"
    / "fixtures"
    / "factory-floor-2026-11-02T1040Z.json"
)
SUFFIX = ".read-at.json"
SCOPE = "floor-fill-test"


def _write_snapshot(
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
                lane_admission_refusal="accounts-unread: token pool command rc=3: no overlay roots",
                engines=(),
            )
        )
    snapshot = ModelLaneHostPoolSnapshot(readings=tuple(readings))
    record = ModelPoolSnapshotRecord(
        correlation_id=uuid.uuid4(),
        read_at=read_at,
        max_age_seconds=180,
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


def _live_section(tmp_path: Path, engine: str = "claude") -> dict[str, Any]:
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
        "pool_engine": engine,
    }


def _recorded_floor(now: datetime) -> tuple[str, dict[str, list[str]]]:
    """The recorded floor of 2026-11-02 (the candidates node's fixture) made current: its text and each line's plan."""
    body = json.loads(RECORDED_FLOOR.read_text(encoding="utf-8"))
    body["generated_at"] = now.isoformat()
    return json.dumps(body), {line["line_id"]: [] for line in body["lines"]}


def _tick(
    tmp_path: Path,
    *,
    free: int,
    age_seconds: int,
    engine: str = "claude",
    bind_seams: bool = True,
    **snapshot: Any,
) -> tuple[str, int, InMemoryFloorStateStore]:
    now = datetime.now(UTC)
    _write_snapshot(
        tmp_path / "pool.json",
        free=free,
        read_at=now - timedelta(seconds=age_seconds),
        **snapshot,
    )
    (tmp_path / "ledger.md").write_text("", encoding="utf-8")
    (tmp_path / "out").mkdir()
    record, plan_lines = _recorded_floor(now)
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
    settings = load_floor_fill_live_settings(_live_section(tmp_path, engine))
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
            **seams,
        ),
    )
    return result.stdout, result.exit_code, store


def _reserved(store: InMemoryFloorStateStore) -> int:
    payload = store.load(SCOPE).payload
    return 0 if payload is None else payload.count('"state":"RESERVED"')


OK = EXIT_CODE_BY_STATUS[EnumFloorFillStatus.OK]
DEGRADED = EXIT_CODE_BY_STATUS[EnumFloorFillStatus.DEGRADED]


def test_measured_cause_sends_a_lane_from_the_pool_snapshot(tmp_path: Path) -> None:
    out, code, store = _tick(tmp_path, free=2, age_seconds=5)
    assert "unbound:pool" not in out
    assert "pool-unread" not in out
    assert code == OK, out
    # A started lane is held RESERVED under its floor key until its CLAIM row is read; two free slots, two starts,
    # whatever the host's cap (5) says.
    assert _reserved(store) == 2, store.load(SCOPE).payload
    assert '"key":"floor:' in (store.load(SCOPE).payload or "")


def test_measured_cause_sends_nothing_from_a_stale_snapshot(tmp_path: Path) -> None:
    out, code, store = _tick(tmp_path, free=2, age_seconds=600)
    assert "pool-unread:pool-snapshot" in out
    assert code == DEGRADED
    assert store.load(SCOPE).version == 0


def test_measured_cause_sends_nothing_when_the_snapshot_has_no_free_slot(
    tmp_path: Path,
) -> None:
    out, code, store = _tick(tmp_path, free=0, age_seconds=5)
    assert "unbound:pool" not in out
    assert "pool-unread" not in out
    assert code == OK, out
    assert _reserved(store) == 0


def test_measured_cause_sends_refuses_when_a_host_read_failed(tmp_path: Path) -> None:
    """The measured h051 reading: the token pool status exits 3 on every host, so the snapshot holds 0 slots and an
    ``accounts-unread`` refusal per host. That is an unread pool, not a free count of zero."""
    out, code, store = _tick(tmp_path, free=0, age_seconds=5, unread_hosts=("h3", "h4"))
    assert "pool-unread:pool-snapshot:" in out, out
    assert "accounts-unread" in out
    assert code == DEGRADED, out
    assert _reserved(store) == 0


def test_measured_cause_sends_refuses_when_one_host_read_failed_beside_a_free_one(
    tmp_path: Path,
) -> None:
    out, code, store = _tick(tmp_path, free=2, age_seconds=5, unread_hosts=("h3",))
    assert "pool-unread:pool-snapshot:" in out, out
    assert code == DEGRADED, out
    assert _reserved(store) == 0


def test_measured_cause_sends_does_not_fill_an_away_host_with_slots(
    tmp_path: Path,
) -> None:
    out, code, store = _tick(tmp_path, free=1, age_seconds=5, away_slots=6)
    assert code == OK, out
    assert _reserved(store) == 1, store.load(SCOPE).payload


def test_measured_cause_sends_counts_lane_slots_not_the_cap(tmp_path: Path) -> None:
    out, code, store = _tick(tmp_path, free=1, age_seconds=5, cap=9)
    assert code == OK, out
    assert _reserved(store) == 1


def test_measured_cause_sends_a_non_claude_engine_fills_its_own_hosts(
    tmp_path: Path,
) -> None:
    out, code, store = _tick(
        tmp_path, free=2, age_seconds=5, engine="codex", engines=("claude", "codex")
    )
    assert code == OK, out
    assert _reserved(store) == 2


def test_measured_cause_sends_a_host_without_the_pool_engine_does_not_fill(
    tmp_path: Path,
) -> None:
    """The snapshot's host offers claude only: a codex fill finds no slot and starts nothing."""
    out, code, store = _tick(
        tmp_path, free=2, age_seconds=5, engine="codex", engines=("claude",)
    )
    assert code == OK, out
    assert _reserved(store) == 0


def test_measured_cause_sends_unbound_live_seams_still_send_nothing(
    tmp_path: Path,
) -> None:
    """The live profile still leaves the tickets, brake and stop seams unbound: the recorded floor with two free slots
    answers an unbound seam by name and starts nothing (AC3 is the pool seam only)."""
    out, code, store = _tick(tmp_path, free=2, age_seconds=5, bind_seams=False)
    assert "unbound:" in out, out
    assert "pool-unread" not in out
    assert code == DEGRADED, out
    assert _reserved(store) == 0


def test_measured_cause_sends_an_overlay_without_the_pool_keys_names_them(
    tmp_path: Path,
) -> None:
    """A live overlay written before the pool keys existed refuses naming each of the four, it never half-loads."""
    section = _live_section(tmp_path)
    for name in (
        "pool_snapshot_path",
        "pool_record_suffix",
        "pool_snapshot_max_age_seconds",
        "pool_engine",
    ):
        del section[name]
    with pytest.raises(FloorFillConfigError) as refused:
        load_floor_fill_live_settings(section)
    assert set(refused.value.keys) == {
        f"floor_fill_live.{name}"
        for name in (
            "pool_snapshot_path",
            "pool_record_suffix",
            "pool_snapshot_max_age_seconds",
            "pool_engine",
        )
    }
