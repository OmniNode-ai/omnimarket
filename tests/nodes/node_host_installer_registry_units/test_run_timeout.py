# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22881: a registry unit's run is limited, so a hung run ends and exits 124 instead of holding the timer job.

On 2026-11-06 the overlay store refresh job hung 40 minutes at 0% CPU; launchd has no run limit for a StartInterval job,
so no tick followed and the job-exit alarm opened a missed-interval episode. A unit that declares run_timeout_s now runs
under a wrapper that ends the command's whole process group at the limit and exits 124.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    TIMEOUT_INTERPRETER,
    HandlerRegistryUnits,
    load_units,
)
from tests.nodes.node_host_installer_registry_units.test_registry_units import (
    BREW_PYTHON,
    UNIT,
    make_checkout,
    plist_of,
    real_run,
    request,
)

ENTRIES = {
    "factory-floor-publish": None,
    "lab-test-harness": "src/omnimarket/nodes/node_lab_test_harness_effect/runtime_lab_test_harness.py",
    "silent-failure-alarm": "src/omnimarket/nodes/node_silent_failure_alarm_effect/runtime_silent_failure_alarm.py",
}


@pytest.fixture(autouse=True)
def brew_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda path: path == BREW_PYTHON,
    )


def render(tmp_path: Path, unit: str, entry: str | None = None) -> dict[str, Any]:
    """The unit rendered from a checkout, with the alarm's configuration inputs its declaration requires."""
    branch = tmp_path / "branch"
    revision = make_checkout(branch, entry=entry) if entry else make_checkout(branch)
    result = HandlerRegistryUnits(real_run).handle(
        request(
            tmp_path,
            unit=unit,
            project_path=str(branch),
            expected_revision=revision,
            appender_root=str(tmp_path / "appender"),
            host_table_path=str(tmp_path / "hosts.yaml"),
        )
    )
    assert result.status == "success", result.stderr
    return plist_of(result.rendered)


def wrapper_of(tmp_path: Path, unit: str) -> tuple[list[str], list[str]]:
    """The rendered wrapper entries (interpreter, -e, program, limit) and the command that follows them."""
    arguments: list[str] = render(tmp_path, unit, ENTRIES[unit])["ProgramArguments"]
    return arguments[:4], arguments[4:]


@pytest.mark.parametrize("unit", sorted(ENTRIES))
def test_every_registry_unit_declares_a_run_limit_under_its_interval(
    tmp_path: Path, unit: str
) -> None:
    declared = load_units()[unit]
    limit = declared["run_timeout_s"]
    assert 0 < limit <= declared["schedule"]["start_interval"] * 3
    wrapper, command = wrapper_of(tmp_path, unit)
    assert wrapper[0] == TIMEOUT_INTERPRETER
    assert wrapper[3] == str(limit)
    assert command[1] == "run"
    assert command[0].endswith("uv")


def test_a_unit_that_declares_no_limit_renders_no_wrapper(tmp_path: Path) -> None:
    units = load_units()
    declared = {k: v for k, v in units[UNIT].items() if k != "run_timeout_s"}
    branch = tmp_path / "branch"
    revision = make_checkout(branch)
    result = HandlerRegistryUnits(real_run, units={**units, UNIT: declared}).handle(
        request(tmp_path, project_path=str(branch), expected_revision=revision)
    )
    assert result.status == "success", result.stderr
    assert TIMEOUT_INTERPRETER not in result.rendered
    assert "<string>-e</string>" not in result.rendered


@pytest.mark.parametrize("bad", [0, -5, "900", True, 1.5])
def test_a_run_limit_that_is_not_a_positive_whole_number_is_refused(
    tmp_path: Path, bad: Any
) -> None:
    units = load_units()
    units[UNIT] = {**units[UNIT], "run_timeout_s": bad}
    branch = tmp_path / "branch"
    revision = make_checkout(branch)
    result = HandlerRegistryUnits(real_run, units=units).handle(
        request(tmp_path, project_path=str(branch), expected_revision=revision)
    )
    assert result.status == "failure"
    assert "run_timeout_s" in result.stderr


def run_wrapped(
    tmp_path: Path, limit: int, shell: str
) -> tuple[subprocess.CompletedProcess[str], float]:
    wrapper, _ = wrapper_of(tmp_path / f"render-{limit}-{len(shell)}", UNIT)
    started = time.monotonic()
    done = subprocess.run(
        [*wrapper[:3], str(limit), "/bin/sh", "-c", shell],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return done, time.monotonic() - started


def test_a_hung_run_ends_at_the_limit_with_its_children_and_exits_124(
    tmp_path: Path,
) -> None:
    child = tmp_path / "child.pid"
    done, seconds = run_wrapped(tmp_path, 1, f"sleep 300 & echo $! > {child}; wait")
    assert done.returncode == 124
    assert seconds < 30
    pid = int(child.read_text(encoding="utf-8"))
    for _ in range(50):
        alive = subprocess.run(
            ["kill", "-0", str(pid)], capture_output=True, check=False
        ).returncode
        if alive != 0:
            break
        time.sleep(0.1)
    assert alive != 0, "the hung run's child outlived the limit"


def test_a_run_that_finishes_inside_the_limit_keeps_its_own_exit_code(
    tmp_path: Path,
) -> None:
    assert run_wrapped(tmp_path, 30, "exit 0")[0].returncode == 0
    assert run_wrapped(tmp_path, 30, "exit 65")[0].returncode == 65
