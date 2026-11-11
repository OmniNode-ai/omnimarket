# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The split test job names the test that crashed the shard (OMN-19998).

A shard can die hard (segfault, exit 139) leaving no summary in the log, so
CI never says which test was running. These tests read this repository's own
ci.yml: the fault-handler env and verbose flags the two pytest steps need to
leave a trail, the exit code surviving the tee pipe, and the OMN-23625
diagnostic step that reads the shard log and names the crashed test.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
CI = ROOT / ".github" / "workflows" / "ci.yml"
SMART = "Run pytest (smart selection)"
FULL = "Run pytest (full suite)"
PYTEST_STEPS = (SMART, FULL)
DIAGNOSTIC = "Name the crashed test (OMN-23625)"
EMIT = "Emit coverage artifact metadata (OMN-14680)"
pytestmark = pytest.mark.unit

FLAGS = (
    '-m "not kafka"',
    "--timeout=300",
    "--timeout-method=thread",
    "--cov=src",
    "--cov-report=",
    "-v",
    "--tb=short",
    "--junitxml=junit-${{ matrix.split }}.xml",
)

_SUDO_STUB = (
    "#!/bin/sh\n"
    "# OMN-19998: stand-in for the runner's sudo; `sudo -n dmesg` reports one\n"
    "# segfault line naming the CPython _yaml module plus one unrelated line.\n"
    "echo 'python3[4242]: segfault at 0 ip 00007f0000001234 sp 00007ffd00000000 "
    "error 4 in _yaml.cpython-312-x86_64-linux-gnu.so[7f0000000000+1a000]'\n"
    "echo 'unrelated kernel line'\n"
)


def _steps() -> list[dict[str, Any]]:
    doc = yaml.safe_load(CI.read_text())
    assert isinstance(doc, dict)
    steps = doc["jobs"]["test"]["steps"]
    assert isinstance(steps, list)
    return steps


def _index_of(steps: list[dict[str, Any]], name: str) -> int:
    indexes = [i for i, step in enumerate(steps) if step.get("name") == name]
    assert len(indexes) == 1, name
    return indexes[0]


def _step(name: str) -> dict[str, Any]:
    steps = _steps()
    return steps[_index_of(steps, name)]


def _step_run(name: str) -> str:
    return str(_step(name)["run"])


def _keeps_exit_code(run: str) -> bool:
    """The test-3 predicate: pytest's status survives the tee pipe."""
    needed = (
        "set -o pipefail",
        'tee "$RUNNER_TEMP/pytest-${{ matrix.split }}.log"',
        "|| rc=$?",
        'exit "$rc"',
        "rc=0",
    )
    return all(part in run for part in needed)


def _without_pipefail(run: str) -> str:
    return run.replace("set -o pipefail", "set -o")


def test_both_pytest_steps_turn_the_fault_handler_on() -> None:
    for name in PYTEST_STEPS:
        env = _step(name).get("env", {})
        assert env["PYTHONFAULTHANDLER"] == "1", name
        assert env["PYTHONUNBUFFERED"] == "1", name


@pytest.mark.parametrize("flag", FLAGS)
@pytest.mark.parametrize("step_name", PYTEST_STEPS)
def test_both_pytest_steps_keep_every_flag(step_name: str, flag: str) -> None:
    assert flag in _step_run(step_name)


def test_both_pytest_steps_keep_the_exit_code_of_pytest() -> None:
    for name in PYTEST_STEPS:
        assert _keeps_exit_code(_step_run(name)), name


def test_diagnostic_step_runs_only_on_failure_and_is_ordered() -> None:
    steps = _steps()
    diag_indexes = [i for i, step in enumerate(steps) if step.get("name") == DIAGNOSTIC]
    assert len(diag_indexes) == 1
    diag_index, step = diag_indexes[0], steps[diag_indexes[0]]
    assert step["if"] == "failure()"
    for name in PYTEST_STEPS:
        assert diag_index > _index_of(steps, name), name
    assert diag_index < _index_of(steps, EMIT)
    assert step["env"]["SPLIT"] == "${{ matrix.split }}"
    assert "continue-on-error" not in step


def _run_diagnostic(
    script: str, tmp_path: Path, log: str | None
) -> subprocess.CompletedProcess[str]:
    runner_temp = tmp_path / "runner-temp"
    bin_dir = tmp_path / "stub-bin"
    runner_temp.mkdir()
    bin_dir.mkdir()
    stub = bin_dir / "sudo"
    stub.write_text(_SUDO_STUB)
    stub.chmod(0o755)
    if log is not None:
        (runner_temp / "pytest-3.log").write_text(log)
    env = dict(os.environ)
    env["RUNNER_TEMP"] = str(runner_temp)
    env["SPLIT"] = "3"
    env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
    return subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        cwd=tmp_path,
        check=False,
    )


def test_diagnostic_names_the_last_started_test_and_kernel_fault(
    tmp_path: Path,
) -> None:
    log = (
        "tests/a/test_one.py::test_ok PASSED [ 10%]\ntests/a/test_two.py::test_dies \n"
    )
    proc = _run_diagnostic(_step_run(DIAGNOSTIC), tmp_path, log)
    assert "last test started: tests/a/test_two.py::test_dies" in proc.stdout
    assert "_yaml.cpython-312" in proc.stdout
    assert "unrelated kernel line" not in proc.stdout
    assert proc.returncode == 0


def test_diagnostic_echoes_the_fatal_python_error_block(tmp_path: Path) -> None:
    log = (
        "tests/a/test_two.py::test_dies \n"
        "Fatal Python error: Segmentation fault\n"
        "\n"
        "Current thread 0x00007f (most recent call first):\n"
        '  File "/x/yaml/loader.py", line 10 in construct\n'
    )
    proc = _run_diagnostic(_step_run(DIAGNOSTIC), tmp_path, log)
    assert "Fatal Python error: Segmentation fault" in proc.stdout
    assert "Current thread 0x00007f (most recent call first):" in proc.stdout
    assert '  File "/x/yaml/loader.py", line 10 in construct' in proc.stdout
    assert proc.returncode == 0


def test_diagnostic_stays_quiet_for_an_ordinary_failure(tmp_path: Path) -> None:
    log = (
        "tests/a/test_one.py::test_ok PASSED [ 10%]\n"
        "tests/a/test_one.py::test_bad FAILED [ 30%]\n"
        "=== 1 failed, 5 passed in 3.21s ===\n"
    )
    proc = _run_diagnostic(_step_run(DIAGNOSTIC), tmp_path, log)
    assert "last test started" not in proc.stdout
    assert "segfault" not in proc.stdout
    assert proc.returncode == 0


def test_diagnostic_survives_a_missing_log(tmp_path: Path) -> None:
    proc = _run_diagnostic(_step_run(DIAGNOSTIC), tmp_path, None)
    assert proc.returncode == 0
    assert "no pytest log" in proc.stdout


def test_exit_code_predicate_control() -> None:
    real = _step_run(FULL)
    assert _keeps_exit_code(real) is True
    assert _keeps_exit_code(_without_pipefail(real)) is False
