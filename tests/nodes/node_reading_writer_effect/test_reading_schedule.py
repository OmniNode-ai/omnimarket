# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The writer's hourly schedule: units from the contract, install, rollback (OMN-22478)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_reading_writer_effect.handlers.schedule import (
    ReadingScheduler,
    load_schedule,
    render_units,
)
from omnimarket.nodes.node_reading_writer_effect.overlay import ModelReadingOverlay
from tests.nodes.node_reading_writer_effect.reading_support import (
    overlay,
    overlay_document,
)

pytestmark = pytest.mark.unit


class _Recorder:
    def __init__(self, failing: str | None = None) -> None:
        self.commands: list[list[str]] = []
        self.failing = failing

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        code = 1 if self.failing and self.failing in command else 0
        return subprocess.CompletedProcess(
            list(command), code, "", "boom" if code else ""
        )


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "root"
    python = root / "venv-readings" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n")
    home = tmp_path / "home"
    home.mkdir()
    return root, home, tmp_path / "overlays"


def _scheduler(home: Path, recorder: _Recorder) -> ReadingScheduler:
    return ReadingScheduler(
        schedule=load_schedule(), overlay=overlay(), home=home, run=recorder
    )


def test_the_units_come_from_the_contract_and_survive_a_reboot(tmp_path: Path) -> None:
    root, home, overlays = _layout(tmp_path)
    service, timer = render_units(
        load_schedule(),
        overlay(),
        omni_root=root,
        venv=root / "venv-readings",
        home=home,
        overlay_root=overlays,
    )
    assert "OnCalendar=*-*-* *:15:00" in timer
    assert "Persistent=true" in timer  # a run missed across a reboot is made up
    assert "WantedBy=timers.target" in timer
    assert "-m omnimarket.nodes.node_reading_writer_effect hourly" in service
    assert "Type=oneshot" in service
    assert f'Environment="ONEX_SKILL_OVERLAY_ROOTS={overlays}"' in service
    assert (
        'Environment="READING_TEST_APPEND=/bin/true"' in service
    )  # route facts come from the overlay


def test_install_writes_the_two_units_and_enables_the_timer(tmp_path: Path) -> None:
    root, home, overlays = _layout(tmp_path)
    recorder = _Recorder()
    result = _scheduler(home, recorder).install(
        "h-test", omni_root=root, overlay_root=overlays
    )
    assert result.exit_code == 0, result.stderr
    unit_dir = home / ".config/systemd/user"
    assert (unit_dir / "onex-reading-hourly.service").is_file()
    assert (unit_dir / "onex-reading-hourly.timer").is_file()
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "onex-reading-hourly.timer",
    ] in recorder.commands
    again = _scheduler(home, _Recorder()).install(
        "h-test", omni_root=root, overlay_root=overlays
    )
    assert "unchanged" in again.stdout


def test_uninstall_is_the_rollback_and_removes_exactly_what_install_wrote(
    tmp_path: Path,
) -> None:
    root, home, overlays = _layout(tmp_path)
    scheduler = _scheduler(home, _Recorder())
    scheduler.install("h-test", omni_root=root, overlay_root=overlays)
    recorder = _Recorder()
    result = _scheduler(home, recorder).uninstall("h-test")
    assert result.exit_code == 0
    assert list((home / ".config/systemd/user").iterdir()) == []
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "onex-reading-hourly.timer",
    ] in recorder.commands


def test_a_host_the_overlay_does_not_declare_is_refused(tmp_path: Path) -> None:
    root, home, overlays = _layout(tmp_path)
    scheduler = _scheduler(home, _Recorder())
    for result in (
        scheduler.install("elsewhere", omni_root=root, overlay_root=overlays),
        scheduler.uninstall("elsewhere"),
        scheduler.run_now("elsewhere"),
    ):
        assert result.exit_code == 1
        assert "not in the node_reading_writer_effect overlay" in result.stderr
    assert not (home / ".config").exists()


def test_install_refuses_when_the_node_cannot_start_there(tmp_path: Path) -> None:
    root, home, overlays = _layout(tmp_path)
    recorder = _Recorder(failing="-c")
    result = _scheduler(home, recorder).install(
        "h-test", omni_root=root, overlay_root=overlays
    )
    assert result.exit_code == 1
    assert "would not start" in result.stderr
    assert not (home / ".config").exists()


def test_a_dry_run_writes_nothing(tmp_path: Path) -> None:
    root, home, overlays = _layout(tmp_path)
    recorder = _Recorder()
    result = _scheduler(home, recorder).install(
        "h-test", omni_root=root, overlay_root=overlays, dry_run=True
    )
    assert result.exit_code == 0
    assert "[Timer]" in result.stdout
    assert recorder.commands == []
    assert not (home / ".config").exists()


def test_the_installed_unit_runs_the_overlays_venv_never_a_dispatch_venv(
    tmp_path: Path,
) -> None:
    root, home, overlays = _layout(tmp_path)
    recorder = _Recorder()
    done = _scheduler(home, recorder).install(
        "h-test", omni_root=root, overlay_root=overlays
    )
    assert done.exit_code == 0, done.stderr
    service = (home / ".config/systemd/user/onex-reading-hourly.service").read_text()
    assert f'ExecStart="{root}/venv-readings/bin/python"' in service
    assert "dispatch" not in service
    assert [str(root / "venv-readings/bin/python"), "-c"] == recorder.commands[0][:2]


def test_a_host_declared_without_a_venv_is_refused_by_the_overlay() -> None:
    document = overlay_document()
    document["schedule_hosts"] = [{"name": "h-test", "scheduler": "systemd-user"}]
    with pytest.raises(ValidationError):
        ModelReadingOverlay.model_validate(document)
