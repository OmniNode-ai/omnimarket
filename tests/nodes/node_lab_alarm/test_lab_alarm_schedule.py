# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab alarm's schedule: a launchd agent or systemd timer from the overlay's schedule block."""

from __future__ import annotations

import plistlib
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_lab_alarm import runtime_lab_alarm
from omnimarket.nodes.node_lab_alarm.handlers.schedule import (
    LabAlarmScheduler,
    render_launchd,
    render_systemd,
)
from omnimarket.nodes.node_lab_alarm.overlay import (
    ModelLabAlarmOverlay,
    ModelLabAlarmSchedule,
)

_NODE = Path(runtime_lab_alarm.__file__).parent
_MAC = "example-mac"
_LINUX = "example-linux"


def _raw() -> dict[str, object]:
    loaded = yaml.safe_load(
        (_NODE / "overlay.example.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(loaded, dict)
    return loaded


def _overlay() -> ModelLabAlarmOverlay:
    return ModelLabAlarmOverlay.model_validate(_raw())


class _Runner:
    """Records each command; the import probe and the scheduler commands succeed unless told."""

    def __init__(self, fail_on: str = "") -> None:
        self.commands: list[list[str]] = []
        self.fail_on = fail_on

    def __call__(self, command: Sequence[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(list(command))
        code = 1 if self.fail_on and self.fail_on in " ".join(command) else 0
        return subprocess.CompletedProcess(
            list(command), code, "", "boom" if code else ""
        )


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "registry"
    python = root / "examplepkg/.venv/bin/python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("", encoding="utf-8")
    return root


def _scheduler(tmp_path: Path, runner: _Runner) -> LabAlarmScheduler:
    return LabAlarmScheduler(
        overlay=_overlay(), home=tmp_path / "home", run=runner, uid=501
    )


def test_golden_launchd_install_writes_a_300_second_agent_and_loads_it(
    tmp_path: Path,
) -> None:
    root, runner = _root(tmp_path), _Runner()
    scheduler = _scheduler(tmp_path, runner)
    result = scheduler.install(_MAC, omni_root=root, overlay_root=tmp_path / "overlays")
    assert result.exit_code == 0, result.stderr
    agent = plistlib.loads(scheduler.plist.read_bytes())
    assert agent["StartInterval"] == 300
    assert agent["ProgramArguments"][1:] == [
        "-m",
        "omnimarket.nodes.node_lab_alarm",
        "broker-lag",
    ]
    assert agent["WorkingDirectory"] == str(root)
    assert agent["EnvironmentVariables"]["EXAMPLE_SETTING"] == f"{root}/example"
    assert [c[1] for c in runner.commands[1:]] == ["bootout", "bootstrap"]
    assert runner.commands[-1][2:] == ["gui/501", str(scheduler.plist)]


def test_golden_dry_run_writes_nothing_and_prints_the_agent(tmp_path: Path) -> None:
    root, runner = _root(tmp_path), _Runner()
    scheduler = _scheduler(tmp_path, runner)
    result = scheduler.install(
        _MAC, omni_root=root, overlay_root=tmp_path / "overlays", dry_run=True
    )
    assert result.exit_code == 0
    assert "<key>StartInterval</key>" in result.stdout
    assert not scheduler.plist.exists()
    assert runner.commands == []


def test_golden_systemd_timer_uses_the_overlay_interval(tmp_path: Path) -> None:
    service, timer = render_systemd(
        _overlay(),
        _overlay().schedule.hosts[1],
        omni_root=_root(tmp_path),
        home=tmp_path / "home",
        overlay_root=tmp_path / "overlays",
    )
    assert "OnUnitActiveSec=300s" in timer
    assert "node_lab_alarm broker-lag" in service


def test_golden_uninstall_boots_out_and_removes_the_agent(tmp_path: Path) -> None:
    root, runner = _root(tmp_path), _Runner()
    scheduler = _scheduler(tmp_path, runner)
    scheduler.install(_MAC, omni_root=root, overlay_root=tmp_path / "overlays")
    runner.commands.clear()
    result = scheduler.uninstall(_MAC)
    assert result.exit_code == 0
    assert not scheduler.plist.exists()
    assert runner.commands == [["launchctl", "bootout", "gui/501/example.lab-alarm"]]


def test_error_a_host_the_overlay_does_not_declare_is_refused(tmp_path: Path) -> None:
    scheduler = _scheduler(tmp_path, _Runner())
    result = scheduler.install(
        "unknown", omni_root=_root(tmp_path), overlay_root=tmp_path / "o"
    )
    assert result.exit_code == 1
    assert "not in the node_lab_alarm overlay's schedule hosts" in result.stderr


def test_error_a_registry_root_under_worktrees_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "omni_worktrees" / "lane" / "registry"
    python = root / "examplepkg/.venv/bin/python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("", encoding="utf-8")
    runner = _Runner()
    result = _scheduler(tmp_path, runner).install(
        _MAC, omni_root=root, overlay_root=tmp_path / "o"
    )
    assert result.exit_code == 1
    assert "omni_worktrees" in result.stderr
    assert runner.commands == []


def test_error_an_interpreter_that_cannot_import_the_node_is_refused(
    tmp_path: Path,
) -> None:
    runner = _Runner(fail_on="import omnimarket")
    scheduler = _scheduler(tmp_path, runner)
    result = scheduler.install(
        _MAC, omni_root=_root(tmp_path), overlay_root=tmp_path / "o"
    )
    assert result.exit_code == 1
    assert "cannot import" in result.stderr
    assert not scheduler.plist.exists()


def test_error_a_failed_bootstrap_is_reported_with_its_exit_code(
    tmp_path: Path,
) -> None:
    result = _scheduler(tmp_path, _Runner(fail_on="launchctl bootstrap")).install(
        _MAC, omni_root=_root(tmp_path), overlay_root=tmp_path / "o"
    )
    assert result.exit_code == 1
    assert result.stderr == "boom"


def test_error_an_overlay_without_a_schedule_is_refused() -> None:
    raw = _raw()
    del raw["schedule"]
    with pytest.raises(ValidationError, match="schedule"):
        ModelLabAlarmOverlay.model_validate(raw)


def test_control_an_interval_under_a_minute_is_refused() -> None:
    schedule = dict(_raw()["schedule"])  # type: ignore[call-overload]
    schedule["interval_seconds"] = 30
    with pytest.raises(ValidationError, match="interval_seconds"):
        ModelLabAlarmSchedule.model_validate(schedule)


def test_control_the_rendered_interval_follows_the_overlay(tmp_path: Path) -> None:
    raw = _raw()
    raw["schedule"]["interval_seconds"] = 600  # type: ignore[index]
    overlay = ModelLabAlarmOverlay.model_validate(raw)
    agent = plistlib.loads(
        render_launchd(
            overlay,
            overlay.schedule.hosts[0],
            omni_root=_root(tmp_path),
            home=tmp_path / "home",
            overlay_root=tmp_path / "o",
        )
    )
    assert agent["StartInterval"] == 600


def test_cli_schedule_dry_run_exits_with_the_result_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _root(tmp_path)
    code = runtime_lab_alarm.run_schedule(
        "install",
        _MAC,
        overlay=_overlay(),
        scheduler=_scheduler(tmp_path, _Runner()),
        omni_root=root,
        overlay_root=tmp_path / "o",
        dry_run=True,
    )
    assert code == 0
    assert "StartInterval" in capsys.readouterr().out
    refused = runtime_lab_alarm.run_schedule(
        "install",
        "unknown",
        overlay=_overlay(),
        scheduler=_scheduler(tmp_path, _Runner()),
        omni_root=_root(tmp_path),
        overlay_root=tmp_path / "o",
    )
    assert refused == 1
