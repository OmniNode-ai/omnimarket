# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Review findings 5347:2, 5347:5, 5347:13 and 5347:16 on the commitment check runner (OMN-23013); each failed before the fix."""

from __future__ import annotations

import os
import sys
import tracemalloc
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_commitment_check_runner_effect.adapters.executors import (
    InMemoryCheckExecutor,
    SubprocessCheckExecutor,
)
from omnimarket.nodes.node_commitment_check_runner_effect.handlers.handler_commitment_check_runner import (
    HandlerCommitmentCheckRunner,
)
from omnimarket.nodes.node_commitment_check_runner_effect.models import (
    ModelCheckExecution,
    ModelCommitmentCheckCommand,
)
from omnimarket.nodes.node_commitment_check_runner_effect.runtime_commitment_check import (
    main,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_commitment_check_runner_effect.support import (
    NODES,
    RUN_AT,
    ledger_rules,
    open_commitment_line,
    runner_rules,
)

pytestmark = pytest.mark.unit

FAIL = ModelCheckExecution(exit_code=1)
PASS = ModelCheckExecution(exit_code=0)


def _check(n: int) -> str:
    return f"uv run onex host-read --host h{n:03d} --expect nvme-root"


def _handler(lines, executor, clock, **changes):
    ledger = InMemoryLedger(lines)
    handler_changes = {
        k: changes.pop(k) for k in ("run_budget_seconds",) if k in changes
    }
    return HandlerCommitmentCheckRunner(
        rules=runner_rules(**changes),
        commitment_rules=ledger_rules(),
        source=ledger,
        appender=ledger,
        executor=executor,
        clock=clock,
        **handler_changes,
    )


def test_5347_2_persistently_failing_checks_do_not_starve_the_later_due_commitments() -> (
    None
):
    lines = [
        open_commitment_line(minutes_ago=600 - i, check=_check(i), lane=f"lane-{i}")
        for i in range(6)
    ]
    executor = InMemoryCheckExecutor({_check(i): FAIL for i in range(6)})
    tick = {"n": 0}
    handler = _handler(
        lines,
        executor,
        lambda: RUN_AT + timedelta(seconds=600 * tick["n"]),
        max_checks_per_run=2,
    )
    for tick["n"] in range(14):
        handler.handle(ModelCommitmentCheckCommand(correlation_id=f"run-{tick['n']}"))
    checked = {" ".join(call) for call in executor.calls}
    assert checked == {_check(i) for i in range(6)}


def test_5347_2_a_refused_check_does_not_use_a_slot_of_the_cap() -> None:
    lines = [
        open_commitment_line(minutes_ago=600, check="rm -rf /tmp/x", lane="lane-a"),
        open_commitment_line(minutes_ago=590, check="rm -rf /tmp/y", lane="lane-b"),
        open_commitment_line(minutes_ago=580, check=_check(9), lane="lane-c"),
    ]
    executor = InMemoryCheckExecutor({_check(9): PASS})
    handler = _handler(lines, executor, lambda: RUN_AT, max_checks_per_run=2)
    result = handler.handle(ModelCommitmentCheckCommand(correlation_id="run"))
    assert [o.result for o in result.outcomes if o.ran] == ["verified"]


def test_5347_2_failing_control_the_cap_still_bounds_the_checks_run() -> None:
    lines = [
        open_commitment_line(minutes_ago=600 - i, check=_check(i), lane=f"lane-{i}")
        for i in range(6)
    ]
    executor = InMemoryCheckExecutor({_check(i): FAIL for i in range(6)})
    handler = _handler(lines, executor, lambda: RUN_AT, max_checks_per_run=2)
    handler.handle(ModelCommitmentCheckCommand(correlation_id="run"))
    assert len(executor.calls) == 2


class _SlowExecutor(InMemoryCheckExecutor):
    def __init__(self, results, seconds_per_check: int, clock_state: dict) -> None:
        super().__init__(results)
        self._seconds = seconds_per_check
        self._state = clock_state

    def run(self, argv: Sequence[str], timeout_seconds: int) -> ModelCheckExecution:
        self._state["elapsed"] += self._seconds
        return super().run(argv, timeout_seconds)


def test_5347_16_a_run_stops_starting_checks_the_service_deadline_would_cut_off() -> (
    None
):
    lines = [
        open_commitment_line(minutes_ago=600 - i, check=_check(i), lane=f"lane-{i}")
        for i in range(10)
    ]
    state = {"elapsed": 0}
    executor = _SlowExecutor({_check(i): FAIL for i in range(10)}, 120, state)
    handler = _handler(
        lines,
        executor,
        lambda: RUN_AT + timedelta(seconds=state["elapsed"]),
        max_checks_per_run=10,
        check_timeout_seconds=120,
        run_budget_seconds=600,
    )
    result = handler.handle(ModelCommitmentCheckCommand(correlation_id="run"))
    assert len(executor.calls) == 5, "five 120 s checks fill a 600 s deadline"
    assert result.deferred == 5


def test_5347_16_failing_control_no_budget_runs_the_whole_cap() -> None:
    lines = [
        open_commitment_line(minutes_ago=600 - i, check=_check(i), lane=f"lane-{i}")
        for i in range(10)
    ]
    state = {"elapsed": 0}
    executor = _SlowExecutor({_check(i): FAIL for i in range(10)}, 120, state)
    handler = _handler(
        lines,
        executor,
        lambda: RUN_AT + timedelta(seconds=state["elapsed"]),
        max_checks_per_run=10,
    )
    result = handler.handle(ModelCommitmentCheckCommand(correlation_id="run"))
    assert len(executor.calls) == 10
    assert result.deferred == 0


NOISY = "import sys\nfor _ in range(300):\n    sys.stdout.write('x' * 100000 + '\\n')\nsys.stderr.write('TAIL-MARK')\n"


def test_5347_5_a_noisy_check_is_not_buffered_whole_in_memory() -> None:
    tracemalloc.start()
    try:
        execution = SubprocessCheckExecutor(40).run([sys.executable, "-c", NOISY], 60)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert execution.exit_code == 0
    assert execution.output_tail.endswith("TAIL-MARK")
    assert len(execution.output_tail) <= 40
    assert peak < 8_000_000, f"{peak} bytes held while the check ran"


def test_5347_5_failing_control_a_zero_tail_still_drains_the_child() -> None:
    execution = SubprocessCheckExecutor(0).run([sys.executable, "-c", NOISY], 60)
    assert execution.exit_code == 0
    assert execution.output_tail == ""


def test_5347_5_a_check_past_its_timeout_is_killed_and_did_not_pass() -> None:
    execution = SubprocessCheckExecutor(40).run(
        [sys.executable, "-c", "import time\ntime.sleep(30)"], 1
    )
    assert execution.timed_out
    assert execution.exit_code is None


def _pinned_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(
        "ONEX_COMMITMENT_CHECK_OVERLAY",
        str(NODES / "node_commitment_check_runner_effect/overlay.example.yaml"),
    )
    monkeypatch.setenv(
        "ONEX_COMMITMENT_OVERLAY",
        str(NODES / "node_commitment_ledger_compute/overlay.example.yaml"),
    )
    monkeypatch.delenv("ONEX_SKILL_OVERLAY_ROOTS", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


def test_5347_13_a_pinned_only_deployment_installs_and_the_unit_carries_the_pins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _pinned_env(monkeypatch, tmp_path)
    code = main(
        [
            "schedule",
            "install",
            "--host",
            "example-lab-host",
            "--workspace-root",
            str(tmp_path),
            "--dry-run",
        ]
    )
    out = capsys.readouterr()
    assert code == 0, out.err
    assert "ONEX_COMMITMENT_CHECK_OVERLAY=" in out.out
    assert "ONEX_COMMITMENT_OVERLAY=" in out.out


def test_5347_13_the_unit_keeps_every_overlay_root_not_only_the_first(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _pinned_env(monkeypatch, tmp_path)
    monkeypatch.delenv("ONEX_COMMITMENT_CHECK_OVERLAY")
    first, second = tmp_path / "roots-a", tmp_path / "roots-b"
    runner_dir = first / "node_commitment_check_runner_effect"
    runner_dir.mkdir(parents=True)
    (runner_dir / "overlay.yaml").write_text(
        (NODES / "node_commitment_check_runner_effect/overlay.example.yaml").read_text(
            encoding="utf-8"
        ),
        encoding="utf-8",
    )
    second.mkdir()
    monkeypatch.setenv(
        "ONEX_SKILL_OVERLAY_ROOTS", os.pathsep.join((str(first), str(second)))
    )
    code = main(
        [
            "schedule",
            "install",
            "--host",
            "example-lab-host",
            "--workspace-root",
            str(tmp_path),
            "--dry-run",
        ]
    )
    out = capsys.readouterr()
    assert code == 0, out.err
    assert str(first) in out.out
    assert str(second) in out.out
