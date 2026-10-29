# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-21326 AC3: the deployed-lane readback fails when a run is over, or when nothing was measured."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tests.unit.delegation import omn21326_ac3_budget_bound as readback

NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)

pytestmark = pytest.mark.unit


def _run(
    root: Path,
    run_id: str,
    *,
    duration_ms: int,
    execution_timeout: int = 50,
    requested: int | None = 50,
    locus: str = "deployed-lane",
    status: str = "completed",
    age: timedelta = timedelta(hours=1),
) -> None:
    run_dir = root / "runs" / run_id
    run_dir.mkdir(parents=True)
    budget = {
        "requested_timeout_seconds": requested,
        "execution_timeout_seconds": execution_timeout,
        "task_class_timeout_ceiling_seconds": 240,
        "terminal_delivery_margin_seconds": 60,
    }
    receipt = {
        "locus": locus,
        "receipt": {
            "result": {
                "status": status,
                "execution_duration_ms": duration_ms,
                "budget_evidence": budget,
            }
        },
    }
    (run_dir / "receipt.json").write_text(json.dumps(receipt), encoding="utf-8")
    run_json = run_dir / "run.json"
    run_json.write_text("{}", encoding="utf-8")
    stamp = (NOW - age).timestamp()
    os.utime(run_json, (stamp, stamp))


def _live_test(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the live AC3 test exactly as the lab does, against fixture receipts."""
    monkeypatch.setenv("OMN21326_STATE_ROOT", str(root))
    monkeypatch.setenv("OMN21326_UNTIL", NOW.isoformat())
    monkeypatch.delenv("OMN21326_SINCE", raising=False)
    readback.test_deployed_lane_runs_stay_inside_budget_plus_grace()


def test_the_declared_grace_is_one_value() -> None:
    assert readback.declared_grace_seconds() == 15


def test_runs_inside_the_bound_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(tmp_path, "a", duration_ms=49_000)
    _run(tmp_path, "b", duration_ms=65_000)

    _live_test(tmp_path, monkeypatch)


def test_a_completed_run_past_budget_plus_grace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(tmp_path, "inside", duration_ms=49_000)
    _run(tmp_path, "over", duration_ms=65_001)

    with pytest.raises(AssertionError, match="past budget plus grace") as raised:
        _live_test(tmp_path, monkeypatch)

    assert '"run_id": "over"' in str(raised.value)
    assert '"bound_ms": 65000' in str(raised.value)


def test_an_empty_window_measures_nothing_and_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(tmp_path, "before-window", duration_ms=99_000, age=timedelta(hours=30))
    _run(tmp_path, "in-process", duration_ms=99_000, locus="in-process")
    _run(tmp_path, "unrequested", duration_ms=99_000, requested=None)
    _run(tmp_path, "timed-out", duration_ms=99_000, status="failed")

    with pytest.raises(AssertionError, match="no completed deployed-lane run"):
        _live_test(tmp_path, monkeypatch)


def test_a_missing_or_unset_state_root_cannot_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(readback.ReadbackUnrunnableError, match="not found"):
        _live_test(tmp_path / "absent", monkeypatch)

    monkeypatch.delenv("OMN21326_STATE_ROOT")
    with pytest.raises(readback.ReadbackUnrunnableError, match="OMN21326_STATE_ROOT"):
        readback.test_deployed_lane_runs_stay_inside_budget_plus_grace()


def test_disagreeing_graces_cannot_pass(tmp_path: Path) -> None:
    contracts = tmp_path / "contracts.yaml"
    contracts.write_text(
        "execution_budgets:\n"
        "  a: {requested_timeout_grace_seconds: 15}\n"
        "  b: {requested_timeout_grace_seconds: 20}\n",
        encoding="utf-8",
    )

    with pytest.raises(readback.ReadbackUnrunnableError):
        readback.declared_grace_seconds(contracts)
