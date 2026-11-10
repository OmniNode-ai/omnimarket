# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The validator estate reading's daily schedule (OMN-23080): golden install, error refusals, and a named break.

The row stays fresh on the floor for ``publish_max_age_seconds``; a timer that reads once a day must read
inside that bound, and the schedule check says so when it does not.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.nodes.node_validator_estate_reading_effect.handlers.schedule import (
    DAILY_PERIOD_SECONDS,
    EstateScheduler,
    ModelEstateSchedule,
    load_schedule,
)
from omnimarket.nodes.node_validator_estate_reading_effect.overlay import (
    ModelEstateOverlay,
)

from .test_validator_estate import OVERLAY

pytestmark = pytest.mark.unit

SCHEDULED_HOST = "host-a"
FRESH_FOR_SECONDS = 129600


def _overlay(max_age: int = FRESH_FOR_SECONDS) -> ModelEstateOverlay:
    return ModelEstateOverlay.model_validate(
        {
            **OVERLAY,
            "publish": {**OVERLAY["publish"], "publish_max_age_seconds": max_age},
            "schedule_hosts": [
                {"name": SCHEDULED_HOST, "scheduler": "systemd-user", "venv": "venv"}
            ],
            "unit_environment": {"EXAMPLE_HOST_NAME": SCHEDULED_HOST},
        }
    )


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        return subprocess.CompletedProcess(list(command), 0, "", "")


def _scheduler(
    tmp_path: Path, runner: RecordingRunner, max_age: int = FRESH_FOR_SECONDS
) -> EstateScheduler:
    return EstateScheduler(
        schedule=load_schedule(),
        overlay=_overlay(max_age),
        home=tmp_path / "home",
        run=runner,
    )


def _omni_root(tmp_path: Path) -> Path:
    root = tmp_path / "registry"
    python = root / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    return root


def test_contract_cadence_is_daily_and_inside_the_freshness_bound() -> None:
    schedule = load_schedule()
    assert isinstance(schedule, ModelEstateSchedule)
    assert schedule.cadence.on_calendar.startswith("*-*-* ")
    assert schedule.cadence.persistent is True
    assert DAILY_PERIOD_SECONDS < FRESH_FOR_SECONDS


def test_golden_install_writes_the_units_and_enables_the_timer(tmp_path: Path) -> None:
    runner = RecordingRunner()
    scheduler = _scheduler(tmp_path, runner)
    result = scheduler.install(
        SCHEDULED_HOST,
        omni_root=_omni_root(tmp_path),
        overlay_root=tmp_path / "overlays",
    )
    assert result.exit_code == 0, result.stderr
    unit_dir = tmp_path / "home" / ".config/systemd/user"
    service = (unit_dir / "onex-validator-estate-daily.service").read_text()
    timer = (unit_dir / "onex-validator-estate-daily.timer").read_text()
    assert " -m omnimarket.nodes.node_validator_estate_reading_effect read" in service
    assert "Persistent=true" in timer
    assert ["systemctl", "--user", "enable", "--now", scheduler.timer_name] in (
        runner.calls
    )


def test_uninstall_is_the_rollback(tmp_path: Path) -> None:
    runner = RecordingRunner()
    scheduler = _scheduler(tmp_path, runner)
    scheduler.install(
        SCHEDULED_HOST,
        omni_root=_omni_root(tmp_path),
        overlay_root=tmp_path / "overlays",
    )
    result = scheduler.uninstall(SCHEDULED_HOST)
    assert result.exit_code == 0
    unit_dir = tmp_path / "home" / ".config/systemd/user"
    assert not list(unit_dir.glob("onex-validator-estate-daily.*"))
    assert ["systemctl", "--user", "disable", "--now", scheduler.timer_name] in (
        runner.calls
    )


def test_error_undeclared_host_is_refused(tmp_path: Path) -> None:
    result = _scheduler(tmp_path, RecordingRunner()).install(
        "host-z", omni_root=_omni_root(tmp_path), overlay_root=tmp_path / "overlays"
    )
    assert result.exit_code == 1
    assert "not in the node_validator_estate_reading_effect overlay" in result.stderr


def test_error_unit_run_from_a_worktree_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "omni_worktrees" / "lane" / "registry"
    python = root / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    result = _scheduler(tmp_path, RecordingRunner()).install(
        SCHEDULED_HOST, omni_root=root, overlay_root=tmp_path / "overlays"
    )
    assert result.exit_code == 1
    assert not (tmp_path / "home" / ".config/systemd/user").exists()


def test_check_reads_ok_when_the_row_outlives_the_timer(tmp_path: Path) -> None:
    result = _scheduler(tmp_path, RecordingRunner()).check()
    assert result.exit_code == 0
    assert f"row fresh for {FRESH_FOR_SECONDS} s" in result.stdout


def test_named_break_a_row_that_goes_stale_before_the_next_read_is_a_gap(
    tmp_path: Path,
) -> None:
    result = _scheduler(tmp_path, RecordingRunner(), max_age=3600).check()
    assert result.exit_code == 1
    assert "GAP" in result.stderr
