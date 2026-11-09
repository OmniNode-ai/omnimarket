# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dev deploy reading's schedule: units from the contract, install, rollback (OMN-22958)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_dev_deploy_reading_effect.handlers.schedule import (
    DevDeployReadingScheduler,
    load_schedule,
    render_units,
)
from omnimarket.nodes.node_dev_deploy_reading_effect.overlay import (
    ModelDevDeployReadingOverlay,
    ModelDevDeployScheduleHost,
)

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_dev_deploy_reading_effect/overlay.example.yaml"
)


class _Recorder:
    """A runner that records argv and answers success, so no systemctl ever runs."""

    def __init__(self, failing: str | None = None) -> None:
        self.commands: list[list[str]] = []
        self.failing = failing

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        failed = bool(self.failing and self.failing in command)
        return subprocess.CompletedProcess(
            list(command), 1 if failed else 0, "", "boom" if failed else ""
        )


def _overlay() -> ModelDevDeployReadingOverlay:
    """The shipped example overlay, declaring one test host over a relative venv."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    base = ModelDevDeployReadingOverlay.model_validate(document)
    return base.model_copy(
        update={
            "schedule_hosts": (
                ModelDevDeployScheduleHost(
                    name="h-test", scheduler="systemd-user", venv="venv"
                ),
            )
        }
    )


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "root"
    python = root / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    return root, home, tmp_path / "overlays"


def _scheduler(home: Path, runner: _Recorder) -> DevDeployReadingScheduler:
    return DevDeployReadingScheduler(
        schedule=load_schedule(), overlay=_overlay(), home=home, run=runner
    )


def test_the_schedule_comes_from_the_contract_with_the_hourly_cadence() -> None:
    schedule = load_schedule()
    assert _overlay().schedule_units.unit_name == "example-dev-deploy-reading"
    assert schedule.cadence.on_calendar == "*-*-* *:07:00"


def test_rendered_units_run_the_node_read_with_the_overlays_route_facts(
    tmp_path: Path,
) -> None:
    root, home, overlays = _layout(tmp_path)
    service, timer = render_units(
        load_schedule(),
        _overlay(),
        omni_root=root,
        venv=root / "venv",
        home=home,
        overlay_root=overlays,
    )
    execstart = next(
        line for line in service.splitlines() if line.startswith("ExecStart=")
    )
    assert execstart.endswith("-m omnimarket.nodes.node_dev_deploy_reading_effect read")
    assert f'Environment="OMNI_HOME={root}"' in service
    assert f'Environment="ONEX_SKILL_OVERLAY_ROOTS={overlays}"' in service
    assert 'Environment="ONEX_LEDGER_HOST_NAME=example-host"' in service
    assert "OnCalendar=*-*-* *:07:00" in timer
    assert "Persistent=true" in timer
    assert "Unit=example-dev-deploy-reading.service" in timer


def test_install_for_a_host_the_overlay_does_not_declare_is_refused(
    tmp_path: Path,
) -> None:
    root, home, overlays = _layout(tmp_path)
    runner = _Recorder()
    result = _scheduler(home, runner).install(
        "elsewhere", omni_root=root, overlay_root=overlays
    )
    assert result.exit_code == 1
    assert "not in the node_dev_deploy_reading_effect overlay" in result.stderr
    assert runner.commands == []
    assert not (home / ".config").exists()


def test_install_writes_both_units_under_the_user_directory_and_enables_the_timer(
    tmp_path: Path,
) -> None:
    root, home, overlays = _layout(tmp_path)
    runner = _Recorder()
    result = _scheduler(home, runner).install(
        "h-test", omni_root=root, overlay_root=overlays
    )
    assert result.exit_code == 0, result.stderr
    unit_dir = home / ".config/systemd/user"
    assert (unit_dir / "example-dev-deploy-reading.service").is_file()
    assert (unit_dir / "example-dev-deploy-reading.timer").is_file()
    reload_at = runner.commands.index(["systemctl", "--user", "daemon-reload"])
    enable_at = runner.commands.index(
        [
            "systemctl",
            "--user",
            "enable",
            "--now",
            "example-dev-deploy-reading.timer",
        ]
    )
    assert reload_at < enable_at


def test_a_second_install_with_identical_text_reports_every_file_unchanged(
    tmp_path: Path,
) -> None:
    root, home, overlays = _layout(tmp_path)
    scheduler = _scheduler(home, _Recorder())
    first = scheduler.install("h-test", omni_root=root, overlay_root=overlays)
    assert first.exit_code == 0, first.stderr
    again = scheduler.install("h-test", omni_root=root, overlay_root=overlays)
    assert again.exit_code == 0, again.stderr
    assert again.stdout.count("unchanged") == 4


def test_uninstall_disables_the_timer_and_removes_both_unit_files(
    tmp_path: Path,
) -> None:
    root, home, overlays = _layout(tmp_path)
    _scheduler(home, _Recorder()).install(
        "h-test", omni_root=root, overlay_root=overlays
    )
    runner = _Recorder()
    result = _scheduler(home, runner).uninstall("h-test")
    assert result.exit_code == 0, result.stderr
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "example-dev-deploy-reading.timer",
    ] in runner.commands
    assert list((home / ".config/systemd/user").iterdir()) == []


def test_a_dry_run_writes_nothing_and_returns_both_unit_texts(tmp_path: Path) -> None:
    root, home, overlays = _layout(tmp_path)
    runner = _Recorder()
    result = _scheduler(home, runner).install(
        "h-test", omni_root=root, overlay_root=overlays, dry_run=True
    )
    assert result.exit_code == 0, result.stderr
    assert "[Service]" in result.stdout
    assert "[Timer]" in result.stdout
    assert runner.commands == []
    assert not (home / ".config").exists()


def test_install_refuses_when_the_venv_cannot_import_the_node(tmp_path: Path) -> None:
    root, home, overlays = _layout(tmp_path)
    runner = _Recorder(failing="-c")
    result = _scheduler(home, runner).install(
        "h-test", omni_root=root, overlay_root=overlays
    )
    assert result.exit_code == 1
    assert "would not start" in result.stderr
    assert not (home / ".config").exists()


def test_the_contract_declares_a_daily_drill_unit() -> None:
    drill = load_schedule().drill
    assert drill is not None
    assert (
        _overlay().schedule_units.drill_unit_name == "example-dev-deploy-reading-drill"
    )
    assert drill.cadence.on_calendar == "*-*-* 06:25:00"


def test_install_writes_the_drill_pair_running_the_drill_verb_and_enables_both_timers(
    tmp_path: Path,
) -> None:
    root, home, overlay_root = _layout(tmp_path)
    runner = _Recorder()
    result = _scheduler(home, runner).install(
        "h-test", omni_root=root, overlay_root=overlay_root
    )
    assert result.exit_code == 0, result.stderr
    units = home / ".config/systemd/user"
    drill_service = (units / "example-dev-deploy-reading-drill.service").read_text(
        encoding="utf-8"
    )
    assert drill_service.count("ExecStart=") == 1
    assert "-m omnimarket.nodes.node_dev_deploy_reading_effect drill" in drill_service
    assert f"ONEX_SKILL_OVERLAY_ROOTS={overlay_root}" in drill_service
    assert "dev-deploy-reading-drill.out.log" in drill_service
    drill_timer = (units / "example-dev-deploy-reading-drill.timer").read_text(
        encoding="utf-8"
    )
    assert "OnCalendar=*-*-* 06:25:00" in drill_timer
    hourly = (units / "example-dev-deploy-reading.service").read_text(encoding="utf-8")
    assert "-m omnimarket.nodes.node_dev_deploy_reading_effect read" in hourly
    enabled = [
        c[-1]
        for c in runner.commands
        if c[:4] == ["systemctl", "--user", "enable", "--now"]
    ]
    assert enabled == [
        "example-dev-deploy-reading.timer",
        "example-dev-deploy-reading-drill.timer",
    ]


def test_uninstall_removes_the_drill_pair_too(tmp_path: Path) -> None:
    root, home, overlay_root = _layout(tmp_path)
    runner = _Recorder()
    scheduler = _scheduler(home, runner)
    assert (
        scheduler.install("h-test", omni_root=root, overlay_root=overlay_root).exit_code
        == 0
    )
    runner.commands.clear()
    result = scheduler.uninstall("h-test")
    assert result.exit_code == 0
    assert list((home / ".config/systemd/user").iterdir()) == []


def test_run_now_with_drill_starts_the_drill_service(tmp_path: Path) -> None:
    _, home, _ = _layout(tmp_path)
    runner = _Recorder()
    assert _scheduler(home, runner).run_now("h-test", drill=True).exit_code == 0
    assert runner.commands == [
        ["systemctl", "--user", "start", "example-dev-deploy-reading-drill.service"]
    ]


def test_install_from_a_worktree_venv_is_refused_and_names_the_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "omni_worktrees" / "T" / "omnimarket"
    python = root / "venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir()
    result = _scheduler(home, _Recorder()).install(
        "h-test", omni_root=root, overlay_root=tmp_path / "overlays"
    )
    assert result.exit_code == 1
    assert "omni_worktrees" in result.stderr
    assert not (home / ".config").exists()
