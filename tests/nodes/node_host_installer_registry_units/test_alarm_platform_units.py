# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One silent-failure alarm declaration installs as a launchd job on the Mac and a systemd timer on a Linux host.

The alarm's unit was declared for launchd alone, so an install on a Linux lab host ran ``launchctl``, which is
not there. The declaration now carries a ``systemd`` block beside its launchd fields and the installer selects by
the host platform: the same unit name installs a plist on a Mac and a user service plus timer on Linux, and a Linux
install makes no ``launchctl`` call.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units import (
    HandlerRegistryUnits,
)
from omnimarket.nodes.node_host_installer_registry_units.models.model_registry_units import (
    ModelRegistryUnitsRequest,
)

pytestmark = pytest.mark.unit

HANDLER = "omnimarket.nodes.node_host_installer_registry_units.handlers.handler_registry_units"
UNIT = "silent-failure-alarm"
REVISION = "c" * 40
LINUX_LABEL = "onex-silent-failure-alarm"
MAC_LABEL = "ai.omninode.silent-failure-alarm"


class Recorder:
    """Records every command; git, id and uv answer as a host would, nothing else runs."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(argv)
        if argv[-2:] == ["rev-parse", "HEAD"]:
            return subprocess.CompletedProcess(argv, 0, REVISION + "\n", "")
        if argv[-1] == "-u":
            return subprocess.CompletedProcess(argv, 0, "501\n", "")
        if argv[-1] == "-un":
            return subprocess.CompletedProcess(argv, 0, "fixture-user\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def commands(self, program: str) -> list[list[str]]:
        return [call for call in self.calls if call and call[0] == program]


def _host(monkeypatch: pytest.MonkeyPatch, platform: str) -> None:
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(f"{HANDLER}.interpreter_present", lambda _path: True)
    monkeypatch.setattr(
        "shutil.which", lambda name: "/usr/bin/uv" if name == "uv" else None
    )
    for name in (
        "ONEX_RUNTIME_SSH_HOST",
        "ONEX_ALARM_APPENDER_ROOT",
        "ONEX_ALARM_HOST_TABLE",
    ):
        monkeypatch.delenv(name, raising=False)


def _request(tmp_path: Path, **kw: Any) -> ModelRegistryUnitsRequest:
    fields: dict[str, Any] = {
        "unit": UNIT,
        "home": str(tmp_path / "home"),
        "registry_root": str(tmp_path / "omni"),
        "workspace_config_root": str(tmp_path / "ws"),
        "runtime_ssh_host": "lab-user@runtime.example.test",
        "appender_root": str(tmp_path / "appender"),
        "host_table_path": str(tmp_path / "hosts.yaml"),
        "path_overrides": {
            "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path / "skill-roots" / "primary")
        },
    }
    fields.update(kw)
    return ModelRegistryUnitsRequest(**fields)


def test_linux_host_installs_the_alarm_as_a_systemd_timer_and_calls_no_launchctl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host(monkeypatch, "linux")
    run = Recorder()
    result = HandlerRegistryUnits(run).handle(_request(tmp_path, mode="install"))
    assert result.exit_code == 0, result.stderr
    assert run.commands("launchctl") == []
    unit_dir = tmp_path / "home/.config/systemd/user"
    service = (unit_dir / f"{LINUX_LABEL}.service").read_text(encoding="utf-8")
    timer = (unit_dir / f"{LINUX_LABEL}.timer").read_text(encoding="utf-8")
    assert "Type=oneshot" in service
    assert (
        '"-m" "omnimarket.nodes.node_silent_failure_alarm_effect.runtime_silent_failure_alarm"'
        in service
    )
    assert "ONEX_SILENT_FAILURE_ALARM_CONFIG=" in service
    # The node's own bound (900 s) ends a hung run, as the launchd job's run limit does.
    assert "TimeoutStartSec=900" in service
    assert "OnUnitActiveSec=300" in timer
    assert f"Unit={LINUX_LABEL}.service" in timer
    assert [call[2:] for call in run.commands("systemctl")] == [
        ["daemon-reload"],
        ["enable", f"{LINUX_LABEL}.timer"],
        ["restart", f"{LINUX_LABEL}.timer"],
    ]
    assert not (tmp_path / f"home/Library/LaunchAgents/{MAC_LABEL}.plist").exists()
    config = json.loads(Path(result.config_path).read_text(encoding="utf-8"))
    assert config["registry_root"] == str(tmp_path / "omni")
    record = json.loads(Path(result.record).read_text(encoding="utf-8"))
    assert record["platform"] == "systemd"
    assert record["label"] == LINUX_LABEL
    assert record["config_inputs"]["appender_root"] == str(tmp_path / "appender")


def test_linux_host_dry_run_renders_the_service_timer_and_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host(monkeypatch, "linux")
    run = Recorder()
    result = HandlerRegistryUnits(run).handle(_request(tmp_path))
    assert result.exit_code == 0, result.stderr
    assert "[Timer]" in result.rendered
    assert "<plist" not in result.rendered
    document = json.loads(result.config)
    assert document["registry_root"] == str(tmp_path / "omni")
    assert "ledger_lock" not in document
    assert document["python"].endswith("/.venv/bin/python")
    assert run.commands("launchctl") == []
    assert run.commands("systemctl") == []
    assert not (tmp_path / "home/.config/systemd/user").exists(), (
        "a dry-run writes nothing"
    )


def test_linux_host_kickstart_and_uninstall_use_systemctl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host(monkeypatch, "linux")
    run = Recorder()
    handler = HandlerRegistryUnits(run)
    assert handler.handle(_request(tmp_path, mode="kickstart")).exit_code == 0
    assert handler.handle(_request(tmp_path, mode="uninstall")).exit_code == 0
    assert run.commands("launchctl") == []
    verbs = [call[2:] for call in run.commands("systemctl")]
    assert ["start", "--no-block", f"{LINUX_LABEL}.service"] in verbs
    assert ["disable", "--now", f"{LINUX_LABEL}.timer"] in verbs


def test_mac_host_installs_the_same_declaration_as_a_launchd_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host(monkeypatch, "darwin")
    run = Recorder()
    result = HandlerRegistryUnits(run).handle(_request(tmp_path, mode="install"))
    assert result.exit_code == 0, result.stderr
    assert run.commands("systemctl") == []
    plist = tmp_path / f"home/Library/LaunchAgents/{MAC_LABEL}.plist"
    text = plist.read_text(encoding="utf-8")
    assert f"<string>{MAC_LABEL}</string>" in text
    assert "<integer>300</integer>" in text
    assert [call[:2] for call in run.commands("launchctl")] == [
        ["launchctl", "bootout"],
        ["launchctl", "bootstrap"],
    ]
    assert not (tmp_path / "home/.config/systemd/user").exists()
    record = json.loads(Path(result.record).read_text(encoding="utf-8"))
    assert record["label"] == MAC_LABEL
    assert "platform" not in record


def test_both_platforms_render_from_one_declaration_with_one_module_and_one_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host(monkeypatch, "darwin")
    mac = HandlerRegistryUnits(Recorder()).handle(_request(tmp_path))
    _host(monkeypatch, "linux")
    linux = HandlerRegistryUnits(Recorder()).handle(_request(tmp_path))
    module = (
        "omnimarket.nodes.node_silent_failure_alarm_effect.runtime_silent_failure_alarm"
    )
    assert module in mac.rendered
    assert module in linux.rendered
    assert mac.config == linux.config
    assert mac.config_path == linux.config_path
