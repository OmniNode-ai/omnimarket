# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-21326 AC3: the deployed-lane readback fails by exit status when a run is over."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_delegate_skill_orchestrator import runtime_budget_readback
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers import (
    handler_budget_readback as readback,
)

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


def _main(root: Path, capsys: pytest.CaptureFixture[str]) -> tuple[int, dict]:
    code = runtime_budget_readback.main(
        ["--state-root", str(root), "--until", NOW.isoformat()]
    )
    return code, json.loads(capsys.readouterr().out)


def test_the_declared_grace_is_one_value() -> None:
    assert readback.declared_grace_seconds() == 15


@pytest.mark.parametrize(
    ("state_dir", "state_root", "expected"),
    [
        ("/configured/dir", "/configured/root", "/configured/dir"),
        ("", "/configured/root", "/configured/root"),
        ("  ", " /configured/root ", "/configured/root"),
        ("", "", None),
        ("relative/path", "", None),
    ],
)
def test_default_state_roots_follow_shared_runtime_configuration(
    monkeypatch: pytest.MonkeyPatch,
    state_dir: str,
    state_root: str,
    expected: str | None,
) -> None:
    monkeypatch.setenv("ONEX_STATE_DIR", state_dir)
    monkeypatch.setenv("ONEX_STATE_ROOT", state_root)

    assert readback.default_state_roots() == [
        Path(expected) if expected is not None else Path.home() / ".onex_state"
    ]


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
    module = readback

    with pytest.raises(module.CheckUnrunnableError):
        module.declared_grace_seconds(contracts)


@pytest.mark.parametrize(("duration_ms", "expected"), [(65_000, 0), (65_001, 1)])
def test_deployed_bus_terminal_payload_is_measured(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    duration_ms: int,
    expected: int,
) -> None:
    _run(tmp_path, "bus-terminal", duration_ms=duration_ms)
    receipt_path = tmp_path / "runs" / "bus-terminal" / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    payload = receipt["receipt"]["result"]
    receipt["receipt"]["result"] = {
        "handler_result": None,
        "terminal_payload": {"payload": payload},
    }
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    code, summary = _main(tmp_path, capsys)

    assert code == expected
    assert summary["deployed_lane_completed"] == 1
    assert summary["over_budget_plus_grace"] == (expected == 1)


@pytest.mark.parametrize(
    ("duration_ms", "expected"), [(65_000, 0), (65_001, 1), (None, 2), (None, 3)]
)
def test_packaged_module_preserves_process_exit_status(
    tmp_path: Path, duration_ms: int | None, expected: int
) -> None:
    if duration_ms is not None:
        _run(tmp_path, "selected", duration_ms=duration_ms)
    state_root = tmp_path / "absent" if expected == 3 else tmp_path
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            runtime_budget_readback.__name__,
            "--state-root",
            str(state_root),
            "--until",
            NOW.isoformat(),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == expected, result.stderr
    summary = json.loads(result.stdout)
    if expected == 3:
        assert "state root not found" in summary["error"]
    else:
        assert summary["grace_seconds"] == 15
        assert summary["deployed_lane_completed"] == (duration_ms is not None)
        assert summary["over_budget_plus_grace"] == (expected == 1)
