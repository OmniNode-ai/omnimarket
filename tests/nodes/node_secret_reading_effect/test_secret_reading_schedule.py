# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The secret reading's hourly schedule: units from the contract, install, rollback (OMN-22659)."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_secret_reading_effect.handlers.schedule import (
    SecretReadingScheduler,
    load_schedule,
    render_units,
)
from omnimarket.nodes.node_secret_reading_effect.overlay import (
    ModelSecretReadingOverlay,
    ModelSecretReadingScheduleHost,
)

pytestmark = pytest.mark.unit

EXAMPLE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_secret_reading_effect/overlay.example.yaml"
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


def _overlay() -> ModelSecretReadingOverlay:
    """The shipped example overlay, declaring one test host over a relative venv."""
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    base = ModelSecretReadingOverlay.model_validate(document)
    return base.model_copy(
        update={
            "schedule_hosts": (
                ModelSecretReadingScheduleHost(
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


def _scheduler(home: Path, runner: _Recorder) -> SecretReadingScheduler:
    return SecretReadingScheduler(
        schedule=load_schedule(), overlay=_overlay(), home=home, run=runner
    )


def test_the_schedule_comes_from_the_contract_with_the_hourly_cadence() -> None:
    schedule = load_schedule()
    assert schedule.unit_name == "onex-secret-reading-hourly"
    assert schedule.cadence.on_calendar == "*-*-* *:05:00"


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
    assert execstart.endswith("-m omnimarket.nodes.node_secret_reading_effect read")
    assert f'Environment="OMNI_HOME={root}"' in service
    assert f'Environment="ONEX_SKILL_OVERLAY_ROOTS={overlays}"' in service
    assert 'Environment="ONEX_LEDGER_HOST_NAME=example-host"' in service
    assert "OnCalendar=*-*-* *:05:00" in timer
    assert "Persistent=true" in timer
    assert "Unit=onex-secret-reading-hourly.service" in timer


def test_install_for_a_host_the_overlay_does_not_declare_is_refused(
    tmp_path: Path,
) -> None:
    root, home, overlays = _layout(tmp_path)
    runner = _Recorder()
    result = _scheduler(home, runner).install(
        "elsewhere", omni_root=root, overlay_root=overlays
    )
    assert result.exit_code == 1
    assert "not in the node_secret_reading_effect overlay" in result.stderr
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
    assert (unit_dir / "onex-secret-reading-hourly.service").is_file()
    assert (unit_dir / "onex-secret-reading-hourly.timer").is_file()
    reload_at = runner.commands.index(["systemctl", "--user", "daemon-reload"])
    enable_at = runner.commands.index(
        ["systemctl", "--user", "enable", "--now", "onex-secret-reading-hourly.timer"]
    )
    assert reload_at < enable_at


def test_a_second_install_with_identical_text_reports_both_files_unchanged(
    tmp_path: Path,
) -> None:
    root, home, overlays = _layout(tmp_path)
    scheduler = _scheduler(home, _Recorder())
    first = scheduler.install("h-test", omni_root=root, overlay_root=overlays)
    assert first.exit_code == 0, first.stderr
    again = scheduler.install("h-test", omni_root=root, overlay_root=overlays)
    assert again.exit_code == 0, again.stderr
    assert again.stdout.count("unchanged") == 2


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
        "onex-secret-reading-hourly.timer",
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


def _drill_overlay(drill_root: Path) -> ModelSecretReadingOverlay:
    return _overlay().model_copy(update={"drill_overlay_root": str(drill_root)})


def _drill_layout(tmp_path: Path) -> Path:
    drill_root = tmp_path / "overlays-drill"
    (drill_root / "node_secret_reading_effect").mkdir(parents=True)
    (drill_root / "node_secret_reading_effect" / "overlay.yaml").write_text(
        "drill: true\n", encoding="utf-8"
    )
    return drill_root


def test_the_contract_declares_a_daily_drill_unit() -> None:
    drill = load_schedule().drill
    assert drill is not None
    assert drill.unit_name == "onex-secret-reading-drill-daily"
    assert drill.cadence.on_calendar == "*-*-* 06:20:00"


def test_install_with_a_drill_root_writes_the_drill_pair_and_enables_both_timers(
    tmp_path: Path,
) -> None:
    root, home, overlay_root = _layout(tmp_path)
    drill_root = _drill_layout(tmp_path)
    runner = _Recorder()
    scheduler = SecretReadingScheduler(
        schedule=load_schedule(),
        overlay=_drill_overlay(drill_root),
        home=home,
        run=runner,
    )
    result = scheduler.install("h-test", omni_root=root, overlay_root=overlay_root)
    assert result.exit_code == 0, result.stderr
    units = home / ".config/systemd/user"
    drill_service = (units / "onex-secret-reading-drill-daily.service").read_text(
        encoding="utf-8"
    )
    assert f"ONEX_SKILL_OVERLAY_ROOTS={drill_root}" in drill_service
    assert "secret-reading-drill.out.log" in drill_service
    assert drill_service.rstrip().endswith(
        "SyslogIdentifier=onex-secret-reading-drill-daily"
    )
    drill_timer = (units / "onex-secret-reading-drill-daily.timer").read_text(
        encoding="utf-8"
    )
    assert "OnCalendar=*-*-* 06:20:00" in drill_timer
    assert "Unit=onex-secret-reading-drill-daily.service" in drill_timer
    hourly = (units / "onex-secret-reading-hourly.service").read_text(encoding="utf-8")
    assert f"ONEX_SKILL_OVERLAY_ROOTS={overlay_root}" in hourly
    enabled = [
        c[-1]
        for c in runner.commands
        if c[:4] == ["systemctl", "--user", "enable", "--now"]
    ]
    assert enabled == [
        "onex-secret-reading-hourly.timer",
        "onex-secret-reading-drill-daily.timer",
    ]


def test_install_refuses_a_drill_root_without_its_overlay_and_writes_nothing(
    tmp_path: Path,
) -> None:
    root, home, overlay_root = _layout(tmp_path)
    runner = _Recorder()
    scheduler = SecretReadingScheduler(
        schedule=load_schedule(),
        overlay=_drill_overlay(tmp_path / "missing"),
        home=home,
        run=runner,
    )
    result = scheduler.install("h-test", omni_root=root, overlay_root=overlay_root)
    assert result.exit_code == 1
    assert "no drill overlay" in result.stderr
    assert not (home / ".config/systemd/user").exists()
    assert runner.commands == []


def test_uninstall_removes_the_drill_pair_too(tmp_path: Path) -> None:
    root, home, overlay_root = _layout(tmp_path)
    drill_root = _drill_layout(tmp_path)
    runner = _Recorder()
    scheduler = SecretReadingScheduler(
        schedule=load_schedule(),
        overlay=_drill_overlay(drill_root),
        home=home,
        run=runner,
    )
    assert (
        scheduler.install("h-test", omni_root=root, overlay_root=overlay_root).exit_code
        == 0
    )
    runner.commands.clear()
    result = scheduler.uninstall("h-test")
    assert result.exit_code == 0
    assert list((home / ".config/systemd/user").iterdir()) == []
    disabled = [
        c[-1]
        for c in runner.commands
        if c[:4] == ["systemctl", "--user", "disable", "--now"]
    ]
    assert disabled == [
        "onex-secret-reading-hourly.timer",
        "onex-secret-reading-drill-daily.timer",
    ]


def test_run_now_with_drill_starts_the_drill_service(tmp_path: Path) -> None:
    _, home, _ = _layout(tmp_path)
    runner = _Recorder()
    scheduler = SecretReadingScheduler(
        schedule=load_schedule(),
        overlay=_drill_overlay(_drill_layout(tmp_path)),
        home=home,
        run=runner,
    )
    assert scheduler.run_now("h-test", drill=True).exit_code == 0
    assert runner.commands == [
        ["systemctl", "--user", "start", "onex-secret-reading-drill-daily.service"]
    ]
