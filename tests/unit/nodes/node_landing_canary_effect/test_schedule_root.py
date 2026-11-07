# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC3 (OMN-22920): the canary runs once a day from a lab schedule installed from the registry root, never a worktree."""

from __future__ import annotations

import plistlib
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from omnimarket.handlers.rules_unit_roots import WORKTREE_SEGMENT
from omnimarket.nodes.node_landing_canary_effect.handlers.schedule import (
    LandingCanaryScheduler,
    ModelCanaryScheduleResult,
    load_schedule,
    render_launchd,
    render_systemd,
)
from tests.unit.nodes.node_landing_canary_effect.canary_support import HOST, overlay

pytestmark = pytest.mark.unit

REGISTRY_ROOT = Path("/lab/registry")
WORKTREE_ROOT = REGISTRY_ROOT / WORKTREE_SEGMENT / "OMN-22920-canary" / "repo"
OVERLAY_ROOT = Path("/lab/overlays")
HOME = Path("/home/lab")


class RecordingRunner:
    """Every command the installer runs, each answered with success."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        return subprocess.CompletedProcess(list(command), 0, "", "")


def scheduler(tmp_path: Path, runner: RecordingRunner) -> LandingCanaryScheduler:
    return LandingCanaryScheduler(
        schedule=load_schedule(), overlay=overlay(), home=tmp_path, run=runner
    )


def venv_root(tmp_path: Path) -> Path:
    """A registry root with the venv interpreter the unit would run."""
    root = tmp_path / "registry"
    python = root / overlay().hosts[0].venv / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    return root


def test_the_schedule_is_daily_and_comes_from_the_contract() -> None:
    cadence = load_schedule().cadence
    assert cadence.on_calendar == "*-*-* 04:10:00"
    assert "hourly" not in cadence.on_calendar
    assert (cadence.launchd_hour, cadence.launchd_minute) == (4, 10)
    assert cadence.persistent is True
    assert load_schedule().timeout_seconds > overlay().target.landing_bound_seconds


def test_the_systemd_unit_runs_the_daily_verb_from_the_registry_root() -> None:
    host = overlay().hosts[0]
    service, timer = render_systemd(
        load_schedule(),
        host,
        omni_root=REGISTRY_ROOT,
        home=HOME,
        overlay_root=OVERLAY_ROOT,
    )
    interpreter = f"{REGISTRY_ROOT}/{host.venv}/bin/python"
    assert (
        f"ExecStart={interpreter} -m omnimarket.nodes.node_landing_canary_effect daily"
        in service
    )
    assert f"WorkingDirectory={REGISTRY_ROOT}\n" in service
    assert f'Environment="OMNI_HOME={REGISTRY_ROOT}"' in service
    assert WORKTREE_SEGMENT not in service + timer
    assert "OnCalendar=*-*-* 04:10:00" in timer
    assert "Persistent=true" in timer


def test_the_launchd_agent_runs_once_a_day_from_the_registry_root() -> None:
    host = overlay().hosts[0]
    plist = plistlib.loads(
        render_launchd(
            load_schedule(),
            host,
            omni_root=REGISTRY_ROOT,
            home=HOME,
            overlay_root=OVERLAY_ROOT,
        )
    )
    assert plist["StartCalendarInterval"] == {"Hour": 4, "Minute": 10}
    assert plist["WorkingDirectory"] == str(REGISTRY_ROOT)
    assert plist["ProgramArguments"][-1] == "daily"
    assert WORKTREE_SEGMENT not in str(plist)


def test_an_install_from_the_registry_root_writes_the_units_and_enables_the_timer(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    root = venv_root(tmp_path)
    result = scheduler(tmp_path, runner).install(
        HOST, omni_root=root, overlay_root=OVERLAY_ROOT
    )
    assert result.exit_code == 0, result.stderr
    written = (
        tmp_path / ".config/systemd/user/onex-landing-canary-daily.service"
    ).read_text(encoding="utf-8")
    assert f"WorkingDirectory={root}\n" in written
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "onex-landing-canary-daily.timer",
    ] in (runner.commands)


def test_an_install_from_a_worktree_is_refused_and_writes_nothing(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    result = scheduler(tmp_path, runner).install(
        HOST, omni_root=WORKTREE_ROOT, overlay_root=OVERLAY_ROOT
    )
    assert result.exit_code == 1
    assert WORKTREE_SEGMENT in result.stderr
    assert not (tmp_path / ".config").exists()
    assert runner.commands == []


def test_a_dry_run_from_a_worktree_is_refused_too(tmp_path: Path) -> None:
    result = scheduler(tmp_path, RecordingRunner()).install(
        HOST, omni_root=WORKTREE_ROOT, overlay_root=OVERLAY_ROOT, dry_run=True
    )
    assert result.exit_code == 1
    assert WORKTREE_SEGMENT in result.stderr


def test_an_overlay_root_inside_a_worktree_is_refused(tmp_path: Path) -> None:
    result = scheduler(tmp_path, RecordingRunner()).install(
        HOST,
        omni_root=REGISTRY_ROOT,
        overlay_root=WORKTREE_ROOT / "overlays",
        dry_run=True,
    )
    assert result.exit_code == 1
    assert WORKTREE_SEGMENT in result.stderr


def test_a_relative_root_is_refused(tmp_path: Path) -> None:
    result = scheduler(tmp_path, RecordingRunner()).install(
        HOST, omni_root=Path("registry"), overlay_root=OVERLAY_ROOT, dry_run=True
    )
    assert result.exit_code == 1
    assert "absolute" in result.stderr


def test_a_host_the_overlay_does_not_declare_is_refused(tmp_path: Path) -> None:
    result = scheduler(tmp_path, RecordingRunner()).install(
        "unlisted", omni_root=REGISTRY_ROOT, overlay_root=OVERLAY_ROOT
    )
    assert result.exit_code == 1
    assert "not in the node_landing_canary_effect overlay" in result.stderr


def test_uninstall_is_the_rollback_and_removes_both_units(tmp_path: Path) -> None:
    runner = RecordingRunner()
    sched = scheduler(tmp_path, runner)
    sched.install(HOST, omni_root=venv_root(tmp_path), overlay_root=OVERLAY_ROOT)
    result: ModelCanaryScheduleResult = sched.uninstall(HOST)
    assert result.exit_code == 0
    assert list((tmp_path / ".config/systemd/user").iterdir()) == []
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "onex-landing-canary-daily.timer",
    ] in runner.commands
