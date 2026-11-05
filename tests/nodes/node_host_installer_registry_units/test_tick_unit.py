# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The durable item tick belongs to the host installer, independently of a session."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
    HandlerSystemdRegistryTick,
    load_units,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)


def test_tick_unit_is_declared_for_an_always_on_linux_host() -> None:
    units = load_units()
    assert "item-tick" in units, "the installed units list has no item tick"
    unit = units["item-tick"]
    assert unit["platform"] == "systemd"
    assert unit["schedule"]["start_interval"] == 300
    assert unit["schedule"]["calendar"] == "*-*-* *:0/5:00 UTC"
    assert unit["schedule"]["persistent"] is True
    assert unit["require_linger"] is True
    assert unit["module"] == (
        "omnimarket.nodes.node_item_tick_orchestrator.runtime_item_tick"
    )
    assert unit["entry_file"].endswith("/runtime_item_tick.py")
    assert Path(
        "src/omnimarket/nodes/node_host_installer_registry_units", unit["template"]
    ).is_file()


class Host:
    def __init__(self, now: datetime) -> None:
        self.now = now
        self.age = 30
        self.loaded = "loaded"
        self.active = "active"
        self.enabled = "enabled"
        self.exit_status = "0"
        self.linger = "yes"
        self.commands: list[list[str]] = []
        self.fail = ""

    def run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.commands.append(argv)
        value = ""
        if argv[:2] == ["id", "-un"]:
            value = "testuser\n"
        elif argv[0] == "loginctl":
            assert "testuser" in argv
            value = self.linger + "\n"
        elif "rev-parse" in argv:
            value = "a" * 40
        elif "--property=LoadState,ActiveState,UnitFileState" in argv:
            value = f"LoadState={self.loaded}\nActiveState={self.active}\nUnitFileState={self.enabled}\n"
        elif "--property=ExecMainStartTimestamp" in argv:
            value = (self.now - timedelta(seconds=self.age)).strftime(
                "%a %Y-%m-%d %H:%M:%S UTC"
            )
        elif "--property=ExecMainStatus" in argv:
            value = self.exit_status
        code = 1 if self.fail and self.fail in argv else 0
        return subprocess.CompletedProcess(
            argv, code, value, "injected command failure" if code else ""
        )


def request(tmp_path: Path, mode: str = "dry-run") -> ModelRegistryUnitsRequest:
    return ModelRegistryUnitsRequest(
        unit="item-tick",
        mode=mode,
        home=str(tmp_path / "home"),
        registry_root=str(tmp_path / "registry"),
        workspace_config_root=str(tmp_path / "workspace"),
        project_path=str(tmp_path / "branch"),
        expected_revision="a" * 40,
    )


def installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: Host
) -> HandlerRegistryUnits:
    monkeypatch.setattr(
        "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units.interpreter_present",
        lambda _path: True,
    )
    unit = load_units()["item-tick"]
    entry = tmp_path / "branch" / unit["entry_file"]
    entry.parent.mkdir(parents=True)
    entry.write_text("# installed branch entry\n")
    return HandlerRegistryUnits(run=host.run)


def test_tick_unit_installer_renders_the_branch_and_loads_both_unit_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    host = Host(datetime.now(UTC))
    handler = installer(tmp_path, monkeypatch, host)
    result = handler.handle(request(tmp_path, "install"))
    assert result.exit_code == 0, result.stderr
    assert result.revision == "a" * 40
    assert str(tmp_path / "branch") in result.rendered
    timer = tmp_path / "home/.config/systemd/user/onex-item-tick.timer"
    assert "OnCalendar=*-*-* *:0/5:00 UTC" in timer.read_text()
    service = timer.with_suffix(".service").read_text()
    assert f"WorkingDirectory={tmp_path / 'branch'}\n" in service
    assert "StandardOutput=journal" in service
    assert "--check-config" not in service
    assert "ONEX_ITEM_TICK_OVERLAY=" in service
    assert [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "onex-item-tick.timer",
    ] in host.commands
    assert ["systemctl", "--user", "start", "onex-item-tick.service"] in host.commands
    assert any("--check-config" in argv for argv in host.commands)


@pytest.mark.parametrize("day", [4, 7, 8])
def test_tick_unit_installer_check_reads_last_start_on_weekdays_and_across_a_weekend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, day: int
) -> None:
    host = Host(datetime(2026, 11, day, 12, tzinfo=UTC))
    handler = installer(tmp_path, monkeypatch, host)
    assert handler.handle(request(tmp_path, "install")).exit_code == 0
    result = HandlerSystemdRegistryTick(handler, clock=lambda: host.now).handle(
        request(tmp_path, "status")
    )
    assert result.exit_code == 0, result.stdout
    assert "age_seconds=30.0" in result.stdout
    host.age = 601
    stale = HandlerSystemdRegistryTick(handler, clock=lambda: host.now).handle(
        request(tmp_path, "status")
    )
    assert stale.exit_code == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("loaded", "not-found"),
        ("active", "inactive"),
        ("enabled", "disabled"),
        ("exit_status", "1"),
        ("linger", "no"),
        ("age", -1),
    ],
)
def test_tick_unit_installer_check_refuses_a_stopped_failed_or_non_durable_timer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: str | int
) -> None:
    host = Host(datetime.now(UTC).replace(microsecond=0))
    handler = installer(tmp_path, monkeypatch, host)
    assert handler.handle(request(tmp_path, "install")).exit_code == 0
    setattr(host, field, value)
    result = HandlerSystemdRegistryTick(handler, clock=lambda: host.now).handle(
        request(tmp_path, "status")
    )
    assert result.exit_code == 1


@pytest.mark.parametrize("failure", ["linger", "config"])
def test_tick_unit_refuses_before_writing_when_logout_or_configuration_would_break_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    host = Host(datetime.now(UTC))
    if failure == "linger":
        host.linger = "no"
    else:
        host.fail = "--check-config"
    handler = installer(tmp_path, monkeypatch, host)
    result = handler.handle(request(tmp_path, "install"))
    assert result.exit_code == 1
    assert not (tmp_path / "home/.config/systemd/user").exists()
    assert not any(
        argv[:3] == ["systemctl", "--user", "enable"] for argv in host.commands
    )


def test_tick_unit_systemd_handler_accepts_the_contract_request(tmp_path: Path) -> None:
    import inspect

    parameters = inspect.signature(HandlerSystemdRegistryTick.handle).parameters
    assert tuple(parameters) == ("self", "request")
