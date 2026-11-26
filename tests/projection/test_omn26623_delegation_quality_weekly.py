# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""The delegation-quality weekly exposure (OMN-26623).

W1 a served weekly figure differs from a direct count of the same recorded rows (AC1);
W2 a second rung on the same model is counted as a move down the chain;
W3 a week with no runs for a task type and model serves a rate, or 0%, instead of no rate (AC2);
W4 another tenant's rows are counted.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.models.model_projection_read import ModelProjectionReadRequest
from omnimarket.nodes.node_projection_read_effect.handlers.handler_projection_read import (
    HandlerProjectionRead,
)
from omnimarket.nodes.node_projection_read_effect.ports.sqlite_row_source import (
    SqliteTableRowSource,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.sqlite_database import SqliteDatabaseAdapter

pytestmark = pytest.mark.unit

_WEEKLY = "onex.snapshot.projection.delegation.quality-weekly.v1"
_TENANT = "0af28cd0-0654-42e0-a050-358ee8240940"
_OTHER = "11111111-2222-4333-8444-555555555555"
_ROOT = Path(__file__).resolve().parents[2]
_CONTRACT = _ROOT / "src/omnimarket/nodes/node_projection_delegation/contract.yaml"
_FIXTURE = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "omn26623_recorded_delegation_rows.json"
)


def _topics() -> dict[str, Any]:
    return {
        cfg.topic: cfg
        for cfg in load_projection_exposures_from_contract(
            yaml.safe_load(_CONTRACT.read_text(encoding="utf-8")),
            "node_projection_delegation",
            _CONTRACT,
        )
    }


def _recorded() -> list[dict[str, Any]]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))["rows"]


def _store(tmp_path: Path, rows: list[dict[str, Any]], tenant: str = _TENANT) -> Path:
    path = tmp_path / "delegation.sqlite"
    adapter = SqliteDatabaseAdapter(path)
    for row in rows:
        adapter.upsert(
            "delegation_events",
            "correlation_id",
            {
                **row,
                "tenant_id": tenant,
                "attempt_history": json.dumps(row["attempt_history"]),
            },
        )
    return path


def _served(path: Path, tenant: str = _TENANT) -> list[dict[str, Any]]:
    handler = HandlerProjectionRead(
        topic_map=_topics(), row_source=SqliteTableRowSource(path)
    )
    result = asyncio.run(
        handler.handle(
            ModelProjectionReadRequest(topic=_WEEKLY, tenant_id=tenant, limit=500)
        )
    )
    assert result.http_status == 200, result.response
    rows = (result.response or {})["rows"]
    if not rows:
        return []
    assert len(rows) == 1
    cells: Any = rows[0]["cells"]
    if isinstance(cells, str):
        cells = json.loads(cells)
    return list(cells)


def _cells(rows: list[dict[str, Any]]) -> dict[tuple[str, str, str], dict[str, Any]]:
    return {
        (
            str(row["iso_week"]),
            str(row["task_type"]),
            str(row["model_id"]),
        ): row
        for row in rows
    }


def _direct_count(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str, str], tuple[int, int, int]]:
    counts: dict[tuple[str, str, str], tuple[int, int, int]] = {}
    for row in rows:
        if row["data_source"] == "experiment":
            continue
        attempt_history: list[dict[str, Any]] = row["attempt_history"]
        if not attempt_history:
            continue
        created_at = str(row["created_at"])
        year, week, _ = datetime.fromisoformat(created_at).isocalendar()
        iso_week = f"{year}-W{week:02d}"
        first = attempt_history[0]
        model_id = str(first["model_id"])
        task_type = str(row["task_type"])
        first_try_pass = (
            first["attempt_kind"] == "first_try"
            and first["quality_gate_passed"] is True
            and first["failure_class"] is None
            and bool(row["terminal_ok"])
        )
        moved_down_chain = any(
            attempt["model_id"] != first["model_id"] for attempt in attempt_history[1:]
        )
        key = (iso_week, task_type, model_id)
        runs, passes, moved = counts.get(key, (0, 0, 0))
        counts[key] = (
            runs + 1,
            passes + int(first_try_pass),
            moved + int(moved_down_chain),
        )
    return counts


def test_ac1_each_weekly_figure_equals_a_direct_count_of_the_recorded_rows(
    tmp_path: Path,
) -> None:
    """AC1: every weekly figure must equal an independent direct count of recorded rows."""
    rows = _recorded()

    counted = [
        r for r in rows if r["data_source"] != "experiment" and r["attempt_history"]
    ]
    assert any(
        r["attempt_history"][0]["attempt_kind"] == "first_try"
        and r["attempt_history"][0]["quality_gate_passed"] is True
        and r["attempt_history"][0]["failure_class"] is None
        and bool(r["terminal_ok"])
        for r in counted
    )
    assert any(
        len(r["attempt_history"]) > 1
        and any(
            x["model_id"] != r["attempt_history"][0]["model_id"]
            for x in r["attempt_history"][1:]
        )
        for r in counted
    )
    assert any(
        len(r["attempt_history"]) >= 2
        and len({x["model_id"] for x in r["attempt_history"]}) == 1
        for r in rows
    )
    counted_weeks = {
        f"{datetime.fromisoformat(r['created_at']).isocalendar()[0]}-W{datetime.fromisoformat(r['created_at']).isocalendar()[1]:02d}"
        for r in counted
    }
    assert len(counted_weeks) >= 2

    path = _store(tmp_path, rows)
    served = _cells(_served(path))
    expected = _direct_count(rows)

    assert {k for k, c in served.items() if c["runs"] > 0} == set(expected)
    for key, (runs, passes, moved) in expected.items():
        cell = served[key]
        assert cell["runs"] == runs
        assert cell["first_try_passes"] == passes
        assert cell["moved_down_chain"] == moved
        assert cell["first_try_pass_rate"] == pytest.approx(passes / runs)
        assert cell["moved_down_chain_rate"] == pytest.approx(moved / runs)


def test_ac1_a_second_rung_on_the_same_model_is_not_a_move(tmp_path: Path) -> None:
    """AC1: a second rung on the same model is a redraw, not a move."""
    rows = _recorded()

    redraw_row = next(
        r
        for r in rows
        if len(r["attempt_history"]) >= 2
        and len({a["model_id"] for a in r["attempt_history"]}) == 1
    )
    move_row = next(
        r
        for r in rows
        if len(r["attempt_history"]) >= 2
        and any(
            a["model_id"] != r["attempt_history"][0]["model_id"]
            for a in r["attempt_history"][1:]
        )
    )

    redraw_dir = tmp_path / "redraw"
    redraw_dir.mkdir()
    redraw_path = _store(redraw_dir, [redraw_row])

    move_dir = tmp_path / "move"
    move_dir.mkdir()
    move_path = _store(move_dir, [move_row])

    redraw_cells = [c for c in _cells(_served(redraw_path)).values() if c["runs"] == 1]
    assert len(redraw_cells) == 1
    assert redraw_cells[0]["moved_down_chain"] == 0

    move_cells = [c for c in _cells(_served(move_path)).values() if c["runs"] == 1]
    assert len(move_cells) == 1
    assert move_cells[0]["moved_down_chain"] == 1


def test_ac2_a_week_with_no_runs_serves_no_rate_never_zero(tmp_path: Path) -> None:
    """AC2: a week with no runs serves null rates, never zero."""
    rows = _recorded()
    expected = _direct_count(rows)
    weeks = sorted({k[0] for k in expected})

    pairs = {(k[1], k[2]) for k in expected if k[0] == weeks[0]}
    candidates = [p for p in pairs if (weeks[-1], p[0], p[1]) not in expected]
    assert candidates
    task_type, model_id = candidates[0]

    path = _store(tmp_path, rows)
    served = _cells(_served(path))
    cell = served[(weeks[-1], task_type, model_id)]

    assert cell["runs"] == 0
    assert cell["first_try_passes"] == 0
    assert cell["moved_down_chain"] == 0
    assert cell["first_try_pass_rate"] is None
    assert cell["moved_down_chain_rate"] is None


def test_ac2_an_empty_week_between_two_weeks_with_runs_serves_no_rate(
    tmp_path: Path,
) -> None:
    """Empty week between two weeks with runs serves no rate."""
    rows = _recorded()
    base = None
    for row in rows:
        if row["data_source"] != "experiment" and row["attempt_history"]:
            first = row["attempt_history"][0]
            if (
                first["attempt_kind"] == "first_try"
                and first["quality_gate_passed"] is True
                and first["failure_class"] is None
                and bool(row["terminal_ok"])
            ):
                base = row
                break
    assert base is not None
    later_created = datetime.fromisoformat(base["created_at"]) + timedelta(days=14)
    later = {
        **base,
        "correlation_id": base["correlation_id"] + "-later",
        "created_at": later_created.isoformat(),
    }
    path = _store(tmp_path, [base, later])
    served = _served(path)

    def _week(moment: datetime) -> str:
        year, week, _ = moment.isocalendar()
        return f"{year}-W{week:02d}"

    task_type = base["task_type"]
    model_id = base["attempt_history"][0]["model_id"]
    first_week = _week(datetime.fromisoformat(base["created_at"]))
    middle_week = _week(datetime.fromisoformat(base["created_at"]) + timedelta(days=7))
    later_week = _week(later_created)
    cells = {
        (row["iso_week"], row["task_type"], row["model_id"]): row for row in served
    }
    for week in (first_week, middle_week, later_week):
        assert (week, task_type, model_id) in cells
    middle = cells[(middle_week, task_type, model_id)]
    assert middle["runs"] == 0
    assert middle["first_try_pass_rate"] is None
    assert middle["moved_down_chain_rate"] is None
    for week in (first_week, later_week):
        cell = cells[(week, task_type, model_id)]
        assert cell["runs"] == 1
        assert cell["first_try_pass_rate"] == 1.0


def test_ac2_no_served_cell_pairs_runs_with_a_missing_rate(tmp_path: Path) -> None:
    """No served cell pairs runs with a missing rate."""
    path = _store(tmp_path, _recorded())
    served = _served(path)
    for row in served:
        if row["runs"] == 0:
            assert row["first_try_pass_rate"] is None
            assert row["moved_down_chain_rate"] is None
        else:
            assert row["first_try_pass_rate"] is not None
            assert row["moved_down_chain_rate"] is not None


def test_another_tenants_rows_are_not_counted(tmp_path: Path) -> None:
    """Another tenant's rows are not counted."""
    candidate = None
    for row in _recorded():
        if row["data_source"] != "experiment" and row["attempt_history"]:
            candidate = row
            break
    assert candidate is not None
    other_row = {
        **candidate,
        "correlation_id": candidate["correlation_id"] + "-other",
    }
    path = _store(tmp_path, _recorded(), _TENANT)
    _store(tmp_path, [other_row], _OTHER)
    tenant_cells = _served(path, _TENANT)
    other_cells = _served(path, _OTHER)
    expected_runs = sum(value[0] for value in _direct_count(_recorded()).values())
    assert sum(row["runs"] for row in tenant_cells) == expected_runs
    assert sum(row["runs"] for row in other_cells) == 1


def test_ac1_a_first_rung_that_passed_in_a_run_that_did_not_end_ok_is_not_a_first_try_pass(
    tmp_path: Path,
) -> None:
    """W1: the terminal_ok clause of the first-try definition is applied; no recorded row exercises it, so one recorded pass is copied with terminal_ok 0."""
    recorded = _recorded()
    original = next(
        row
        for row in recorded
        if row["data_source"] != "experiment"
        and bool(row["attempt_history"])
        and row["attempt_history"][0]["attempt_kind"] == "first_try"
        and row["attempt_history"][0]["quality_gate_passed"] is True
        and row["attempt_history"][0]["failure_class"] is None
        and bool(row["terminal_ok"])
    )
    copied = {
        **original,
        "correlation_id": original["correlation_id"] + "-not-ok",
        "terminal_ok": 0,
    }

    not_ok_dir = tmp_path / "not_ok"
    ok_dir = tmp_path / "ok"
    not_ok_dir.mkdir()
    ok_dir.mkdir()

    not_ok_path = _store(not_ok_dir, [copied])
    ok_path = _store(ok_dir, [original])

    not_ok_cells = _cells(_served(not_ok_path))
    ok_cells = _cells(_served(ok_path))

    not_ok_cell = next(cell for cell in not_ok_cells.values() if cell["runs"] == 1)
    ok_cell = next(cell for cell in ok_cells.values() if cell["runs"] == 1)

    assert not_ok_cell["first_try_passes"] == 0
    assert not_ok_cell["first_try_pass_rate"] == 0.0
    assert ok_cell["first_try_passes"] == 1
    assert ok_cell["first_try_pass_rate"] == 1.0

    not_ok_direct = _direct_count([copied])
    for key, (runs, first_try_passes, moved_down_chain) in not_ok_direct.items():
        cell = not_ok_cells[key]
        assert cell["runs"] == runs
        assert cell["first_try_passes"] == first_try_passes
        assert cell["moved_down_chain"] == moved_down_chain

    ok_direct = _direct_count([original])
    for key, (runs, first_try_passes, moved_down_chain) in ok_direct.items():
        cell = ok_cells[key]
        assert cell["runs"] == runs
        assert cell["first_try_passes"] == first_try_passes
        assert cell["moved_down_chain"] == moved_down_chain
