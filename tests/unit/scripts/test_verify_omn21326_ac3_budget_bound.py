# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-21326 AC3: the deployed-lane readback fails by exit status when a run is over."""

from __future__ import annotations

import importlib.util
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "verify_omn21326_ac3_budget_bound.py"
NOW = datetime(2026, 10, 29, 12, 0, tzinfo=UTC)

pytestmark = pytest.mark.unit


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("omn21326_ac3", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


def _main(root: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
    code = _load().main(["--state-root", str(root), "--until", NOW.isoformat()])
    return code, json.loads(capsys.readouterr().out)


def test_the_declared_grace_is_one_value() -> None:
    assert _load().declared_grace_seconds() == 15


def test_runs_inside_the_bound_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(tmp_path, "a", duration_ms=49_000)
    _run(tmp_path, "b", duration_ms=65_000)

    code, summary = _main(tmp_path, capsys)

    assert code == 0
    assert summary["deployed_lane_completed"] == 2
    assert summary["over_budget_plus_grace"] == 0


def test_a_completed_run_past_budget_plus_grace_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(tmp_path, "inside", duration_ms=49_000)
    _run(tmp_path, "over", duration_ms=65_001)

    code, summary = _main(tmp_path, capsys)

    assert code == 1
    assert summary["over_runs"] == [
        {"run_id": "over", "execution_duration_ms": 65_001, "bound_ms": 65_000}
    ]


def test_an_empty_window_measures_nothing_and_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(tmp_path, "before-window", duration_ms=99_000, age=timedelta(hours=30))
    _run(tmp_path, "in-process", duration_ms=99_000, locus="in-process")
    _run(tmp_path, "unrequested", duration_ms=99_000, requested=None)
    _run(tmp_path, "timed-out", duration_ms=99_000, status="failed")

    code, summary = _main(tmp_path, capsys)

    assert code == 2
    assert summary["deployed_lane_completed"] == 0


def test_a_missing_state_root_is_unrunnable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, summary = _main(tmp_path / "absent", capsys)

    assert code == 3
    assert "state root not found" in summary["error"]


def test_disagreeing_graces_are_unrunnable(tmp_path: Path) -> None:
    contracts = tmp_path / "contracts.yaml"
    contracts.write_text(
        "execution_budgets:\n"
        "  a: {requested_timeout_grace_seconds: 15}\n"
        "  b: {requested_timeout_grace_seconds: 20}\n",
        encoding="utf-8",
    )
    module = _load()

    with pytest.raises(module.CheckUnrunnableError):
        module.declared_grace_seconds(contracts)
