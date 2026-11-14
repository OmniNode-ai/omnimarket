# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The Mac lab-fill unit's entry: the golden chain (OMN-24419).

One pass over a real pool snapshot file, the unit's overlay on disk, a selection over a faked bus ledger read
and a tick over faked ports: the snapshot's free slots become the tick's hosts, the select step's holds reach the
tick, and the tick's step line is on stderr. Exit 0 with a lane launched per free slot; a held ticket is skipped.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_lab_fill_effect.handlers.handler_lab_fill_select import (
    HandlerLabFillSelect,
)
from omnimarket.nodes.node_lab_fill_effect.runtime_lab_fill_tick import main
from omnimarket.nodes.node_lane_host_pool_snapshot_effect.handlers.adapter_snapshot_file import (
    FileSnapshotStore,
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

from .test_golden_chain_lab_fill_tick import _row, _tick
from .test_lab_fill_select import FakeLedgerRows

pytestmark = pytest.mark.unit

RECORD_SUFFIX = ".read-at.json"
MAX_AGE_S = 180
HELD_TICKET_LINE = "2026-11-10T10:25:00Z | MSG | from=fix-1 | to=landing-controller | ticket=OMN-1 | pr=repo_a#1"


def tick_overlay(root: Path, **changes: Any) -> dict[str, Any]:
    """The unit's overlay document: synthetic values, every path under ``root``."""
    document: dict[str, Any] = {
        "apply": True,
        "parent": "orchestrator-bb1a",
        "parent_ticket": "OMN-17427",
        "authority_ruling": "2026-10-17T09:03:06Z",
        "authority_lane": "orchestrator-bb1a",
        "landing_lane": "landing-controller",
        "engine": "sonnet",
        "timeout_min": 60,
        "max_lanes": 12,
        "max_per_host": 12,
        "mvp_repos": ["omnimarket"],
        "process_repos": ["process_repo"],
        "alarm_after": 2,
        "diagnose_cooldown_min": 180,
        "park_after": 2,
        "park_hours": 24,
        "cloud": {"enabled": False},
        "host_reach": {},
        "fact_hosts": [],
        "ledger_path": str(root / "ledger.md"),
        "approved_work_path": str(root / "approved-work.json"),
        "brief_dir": str(root / "briefs"),
        "state_path": "",
        "pool_snapshot_path": str(root / "pool.json"),
        "pool_record_suffix": RECORD_SUFFIX,
        "pool_snapshot_max_age_seconds": MAX_AGE_S,
        "pool_engine": "claude_sonnet",
        "selection_ledger_reader": "lab-fill-selection",
        "selection_ledger_window_hours": 6,
        "selection_attempt_limit": 2,
        "selection_attempt_window_hours": 24,
        "selection_cooldown_hours": 6,
        "selection_repeat_limit": 2,
        "selection_scope_repos": [],
    }
    document.update(changes)
    return document


def write_unit(root: Path, **changes: Any) -> dict[str, str]:
    """Write the unit's overlays and the approved-work list under ``root``; the environment that finds them."""
    overlays = root / "overlays"
    (overlays / "node_lab_fill_effect").mkdir(parents=True)
    (overlays / "node_lab_fill_effect" / "overlay.yaml").write_text(
        yaml.safe_dump(tick_overlay(root, **changes)), encoding="utf-8"
    )
    (overlays / "node_lab_fill_selection_compute").mkdir(parents=True)
    (overlays / "node_lab_fill_selection_compute" / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "companion_repositories": ["change_control"],
                "not_work_repositories": ["registry"],
                "document_repositories": ["docs"],
            }
        ),
        encoding="utf-8",
    )
    (root / "approved-work.json").write_text(
        json.dumps({"version": 1, "rows": [_row(1), _row(2), _row(3)]}),
        encoding="utf-8",
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(overlays)}


def write_snapshot(root: Path, *, free_hosts: int = 1, cores: int = 4) -> None:
    """The pool writer's own pass: ``free_hosts`` free hosts of ``cores`` cores (two lane slots each), one away host."""
    path = root / "pool.json"
    names = [f"h{n}" for n in range(1, free_hosts + 1)]
    result = handler(
        ads=tuple(advertisement(name, cores=cores) for name in names),
        rows=(*(host_row(name) for name in names), host_row("away", available=False)),
        accs=tuple(accounts(name) for name in names),
        store=FileSnapshotStore(path, RECORD_SUFFIX),
        overlay_=overlay(snapshot_path=str(path)),
    ).handle(pool_request())
    assert result.record is not None


def clock_at(age_s: int = 0) -> Callable[[], datetime]:
    return lambda: T0 + timedelta(seconds=age_s)


def run(
    env: dict[str, str],
    tmp_path: Path,
    *,
    ledger: FakeLedgerRows | None = None,
    rows: int = 3,
    age_s: int = 0,
) -> tuple[int, str, str, Any]:
    ticker, runner, _ = _tick([_row(n) for n in range(1, rows + 1)])
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["--state-dir", str(tmp_path / "state")],
        environ=env,
        clock=clock_at(age_s),
        selector=HandlerLabFillSelect(
            ledger if ledger is not None else FakeLedgerRows()
        ),
        ticker=ticker,
        stdout=out,
        stderr=err,
    )
    return code, out.getvalue(), err.getvalue(), runner


def test_the_snapshots_free_slots_become_the_ticks_lanes(tmp_path: Path) -> None:
    env = write_unit(tmp_path)
    write_snapshot(tmp_path)
    code, out, err, runner = run(env, tmp_path)
    assert code == 0
    result = json.loads(out)
    assert result["free_slots"] == 2
    assert [(d["ticket"], d["host"]) for d in result["dispatched"]] == [
        ("OMN-1", "h1"),
        ("OMN-2", "h1"),
    ]
    assert len(runner.calls) == 2
    assert "step=run_lab_fill_tick outcome=dispatched free_slots=2 placed=2" in err
    assert "step=run_lab_fill_unit hosts=1 held=0 free=2" in err
    heartbeat = json.loads((tmp_path / "state" / "last-run.json").read_text("utf-8"))
    assert heartbeat["exit_code"] == 0


def test_a_second_free_host_adds_its_slots(tmp_path: Path) -> None:
    """The failing control of the snapshot seam: a tick that ignored the snapshot would keep a fixed slot count."""
    env = write_unit(tmp_path)
    write_snapshot(tmp_path, free_hosts=2)
    code, out, err, runner = run(env, tmp_path)
    assert code == 0
    assert json.loads(out)["free_slots"] == 4
    assert len(runner.calls) == 3  # three approved rows, four free slots
    assert "hosts=2 held=0 free=4" in err


def test_the_selections_hold_skips_its_ticket(tmp_path: Path) -> None:
    env = write_unit(tmp_path)
    write_snapshot(tmp_path)
    code, out, err, runner = run(
        env, tmp_path, ledger=FakeLedgerRows(lines=(HELD_TICKET_LINE,))
    )
    assert code == 0
    result = json.loads(out)
    assert "held=1" in err
    assert [(s["ticket"], s["reason"]) for s in result["skipped"]] == [
        ("OMN-1", "selection:handed-off")
    ]
    assert [d["ticket"] for d in result["dispatched"]] == ["OMN-2", "OMN-3"]
    assert len(runner.calls) == 2


def test_the_probe_loads_nothing(tmp_path: Path) -> None:
    out = io.StringIO()
    assert main(["--probe"], environ={}, stdout=out) == 0
    assert json.loads(out.getvalue()) == {"status": "probe_ok"}
    assert not (tmp_path / "state").exists()


def test_an_overlay_that_does_not_apply_plans_without_launching(tmp_path: Path) -> None:
    """The plan-only read of a pass: the lanes are planned from the snapshot's slots and none is launched."""
    env = write_unit(tmp_path, apply=False)
    write_snapshot(tmp_path)
    code, out, err, runner = run(env, tmp_path)
    assert code == 0
    result = json.loads(out)
    assert result["outcome"] == "planned"
    assert result["free_slots"] == 2
    assert len(result["dispatched"]) == 2
    assert runner.calls == []
    assert "outcome=planned free_slots=2 placed=0" in err
