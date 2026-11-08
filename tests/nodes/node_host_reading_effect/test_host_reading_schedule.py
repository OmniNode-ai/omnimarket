# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The schedule on both schedulers: rendered from the contract, installed, rolled back."""

from __future__ import annotations

import plistlib
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_reading_effect.handlers.schedule import (
    HostReadingScheduler,
    load_schedule,
)
from tests.nodes.node_host_reading_effect.host_reading_support import (
    LEDGER_HOST,
    WRITING_HOST,
    overlay,
)

pytestmark = pytest.mark.unit


class _Recorder:
    def __init__(self, fail: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        code = 1 if self.fail and self.fail in " ".join(command) else 0
        return subprocess.CompletedProcess(list(command), code, "", "")


def _scheduler(tmp_path: Path, run: _Recorder) -> HostReadingScheduler:
    return HostReadingScheduler(
        schedule=load_schedule(),
        overlay=overlay(),
        home=tmp_path / "home",
        run=run,
        uid=501,
    )


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    python = root / "example-venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n")
    return root


def test_systemd_host_installs_a_persistent_timer_and_rolls_back(
    tmp_path: Path,
) -> None:
    run = _Recorder()
    scheduler = _scheduler(tmp_path, run)
    done = scheduler.install(
        WRITING_HOST, omni_root=_root(tmp_path), overlay_root=tmp_path / "overlays"
    )
    assert done.exit_code == 0, done.stderr
    assert scheduler.timer_name in run.calls[-1]
    timer = (scheduler.unit_dir / scheduler.timer_name).read_text()
    service = (scheduler.unit_dir / scheduler.service_name).read_text()
    assert "Persistent=true" in timer
    assert "OnCalendar=*-*-* *:25:00" in timer
    assert "node_host_reading_effect hourly" in service
    assert "ONEX_LEDGER_HOST_NAME=example-writing-host" in service
    assert scheduler.uninstall(WRITING_HOST).exit_code == 0
    assert not (scheduler.unit_dir / scheduler.timer_name).exists()
    assert not (scheduler.unit_dir / scheduler.service_name).exists()


def test_launchd_host_installs_an_agent_and_rolls_back(tmp_path: Path) -> None:
    run = _Recorder()
    scheduler = _scheduler(tmp_path, run)
    root = _root(tmp_path)
    done = scheduler.install(
        LEDGER_HOST, omni_root=root, overlay_root=tmp_path / "overlays"
    )
    assert done.exit_code == 0, done.stderr
    agent = plistlib.loads(scheduler.plist.read_bytes())
    assert agent["Label"] == "ai.omninode.host-reading-hourly"
    assert agent["StartCalendarInterval"] == {"Minute": 25}
    assert agent["ProgramArguments"][-1] == "hourly"
    assert agent["EnvironmentVariables"]["ONEX_LEDGER_HOST_NAME"] == LEDGER_HOST
    assert ["launchctl", "bootstrap", "gui/501", str(scheduler.plist)] in run.calls
    assert scheduler.uninstall(LEDGER_HOST).exit_code == 0
    assert not scheduler.plist.exists()
    assert run.calls[-1] == [
        "launchctl",
        "bootout",
        "gui/501/ai.omninode.host-reading-hourly",
    ]


def test_undeclared_host_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path, _Recorder())
    done = scheduler.install(
        "other", omni_root=_root(tmp_path), overlay_root=tmp_path / "overlays"
    )
    assert done.exit_code == 1
    assert not scheduler.unit_dir.exists()
    assert not scheduler.plist.exists()


def test_interpreter_that_cannot_import_the_node_is_refused_before_a_unit_is_written(
    tmp_path: Path,
) -> None:
    run = _Recorder(fail="import omnimarket")
    scheduler = _scheduler(tmp_path, run)
    done = scheduler.install(
        WRITING_HOST, omni_root=_root(tmp_path), overlay_root=tmp_path / "overlays"
    )
    assert done.exit_code == 1
    assert "cannot import" in done.stderr
    assert not scheduler.unit_dir.exists()


def test_dry_run_prints_and_writes_nothing(tmp_path: Path) -> None:
    run = _Recorder()
    scheduler = _scheduler(tmp_path, run)
    done = scheduler.install(
        WRITING_HOST,
        omni_root=tmp_path / "absent",
        overlay_root=tmp_path / "overlays",
        dry_run=True,
    )
    assert done.exit_code == 0
    assert "OnCalendar" in done.stdout
    assert run.calls == []
    assert not scheduler.unit_dir.exists()


def test_the_installed_unit_runs_the_overlays_venv_never_a_dispatch_venv(
    tmp_path: Path,
) -> None:
    run = _Recorder()
    scheduler = _scheduler(tmp_path, run)
    root = _root(tmp_path)
    done = scheduler.install(
        LEDGER_HOST, omni_root=root, overlay_root=tmp_path / "overlays"
    )
    assert done.exit_code == 0, done.stderr
    agent = plistlib.loads(scheduler.plist.read_bytes())
    assert agent["ProgramArguments"][0] == str(root / "example-venv/bin/python")
    assert "dispatch" not in " ".join(agent["ProgramArguments"])
