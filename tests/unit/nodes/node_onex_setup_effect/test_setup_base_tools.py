# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Installer fault controls, defined before the component implementation.

These fake vendor commands test orchestration, not real package installation.
Falsifiers: verify installs, exit-zero/no-tool claims success, expired sudo
continues into installers, an existing CLT marker is removed, no offered CLT
update leaves our marker, or missing-tool recovery bypasses resource floors.
"""

import subprocess
from pathlib import Path

import pytest

from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_base_tools import (
    HandlerSetupBaseTools,
)
from omnimarket.nodes.node_onex_setup_effect.handlers.handler_setup_preflight import (
    HandlerSetupPreflight,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_command_request import (
    ModelSetupCommandRequest,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_command_result import (
    ModelSetupCommandResult,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_machine_facts import (
    ModelSetupMachineFacts,
)
from omnimarket.nodes.node_onex_setup_effect.models.model_setup_plan import (
    ModelSetupPlan,
)

pytestmark = pytest.mark.unit


def facts(*, clt: bool = True, disk: int = 50) -> ModelSetupMachineFacts:
    return ModelSetupMachineFacts(
        platform="Darwin",
        architecture="arm64",
        macos_version="26.1",
        cpus=8,
        ram_bytes=16 * 1024**3,
        free_disk_bytes=disk * 1024**3,
        administrator=True,
        rosetta=False,
        virtual_machine=False,
        missing_tools=() if clt else ("xcode-command-line-tools",),
    )


class Vendor:
    def __init__(self) -> None:
        self.tools = {
            name: "/fake tools/" + name
            for name in ("git", "gh", "jq", "python3.13", "uv", "brew")
        }
        self.commands: list[tuple[str, ...]] = []
        self.sudo = True
        self.provision = True

    def run(self, request: ModelSetupCommandRequest) -> ModelSetupCommandResult:
        self.commands.append(request.argv)
        status = 0
        if request.argv == ("/usr/bin/sudo", "-n", "true"):
            status = 0 if self.sudo else 1
        if len(request.argv) > 2 and request.argv[1] == "install" and self.provision:
            self.tools[request.argv[2]] = "/fake tools/" + request.argv[2]
        return ModelSetupCommandResult(
            argv=request.argv, returncode=status, stdout="", stderr=""
        )


def adapter(
    tmp_path: Path, vendor: Vendor, *, clt: bool = True
) -> HandlerSetupBaseTools:
    return HandlerSetupBaseTools(
        collect=lambda _: facts(clt=clt),
        locate=vendor.tools.get,
        run=vendor.run,
        clt_marker=tmp_path / "clt-marker",
    )


def test_verify_reports_missing_tool_without_an_installer(tmp_path: Path) -> None:
    vendor = Vendor()
    del vendor.tools["gh"]
    result = adapter(tmp_path, vendor).handle(
        ModelSetupPlan(workspace=tmp_path, action="verify")
    )
    assert result.status == "unavailable"
    assert vendor.commands == []
    assert not list(tmp_path.iterdir())


def test_tool_install_is_reobserved_and_replay_is_unchanged(tmp_path: Path) -> None:
    vendor = Vendor()
    del vendor.tools["gh"]
    component = adapter(tmp_path, vendor)
    plan = ModelSetupPlan(workspace=tmp_path)
    assert component.handle(plan).status == "success"
    assert ("/fake tools/brew", "install", "gh") in vendor.commands
    before = list(vendor.commands)
    assert component.handle(plan).status == "success"
    assert vendor.commands == before


def test_vendor_zero_without_the_tool_is_not_completion(tmp_path: Path) -> None:
    vendor = Vendor()
    del vendor.tools["gh"]
    vendor.provision = False
    result = adapter(tmp_path, vendor).handle(ModelSetupPlan(workspace=tmp_path))
    assert vendor.commands, (
        "positive control must actually invoke the selected installer"
    )
    assert result.status != "success"


def test_missing_sudo_is_a_human_action_before_downloads(tmp_path: Path) -> None:
    vendor = Vendor()
    del vendor.tools["brew"]
    vendor.sudo = False
    result = adapter(tmp_path, vendor).handle(ModelSetupPlan(workspace=tmp_path))
    assert result.status == "action_required"
    assert "sudo -v" in (result.fix or "")
    assert vendor.commands == [("/usr/bin/sudo", "-n", "true")]
    assert not list(tmp_path.iterdir())


def test_existing_clt_marker_is_preserved(tmp_path: Path) -> None:
    marker = tmp_path / "clt-marker"
    marker.write_text("another install owns this\n")
    vendor = Vendor()
    result = adapter(tmp_path, vendor, clt=False).handle(
        ModelSetupPlan(workspace=tmp_path)
    )
    assert result.status == "action_required"
    assert marker.read_text() == "another install owns this\n"
    assert not any(
        "softwareupdate" in str(arg) for command in vendor.commands for arg in command
    )


def test_no_clt_update_cleans_only_its_own_marker(tmp_path: Path) -> None:
    vendor = Vendor()
    result = adapter(tmp_path, vendor, clt=False).handle(
        ModelSetupPlan(workspace=tmp_path)
    )
    assert result.status != "success"
    assert any(
        "softwareupdate" in str(arg) for command in vendor.commands for arg in command
    )
    assert not (tmp_path / "clt-marker").exists()


def test_installable_missing_tools_do_not_bypass_disk_floor(tmp_path: Path) -> None:
    result = HandlerSetupPreflight(
        collect=lambda _: facts(disk=9), require_tools=False
    ).handle(ModelSetupPlan(workspace=tmp_path))
    assert result.status != "success"
    assert "10 GiB" in (result.fix or "")


def test_probe_timeout_is_typed_without_echoing_probe_output(tmp_path: Path) -> None:
    def fail(_workspace: Path) -> ModelSetupMachineFacts:
        raise subprocess.TimeoutExpired("probe", 1, output="credential-SENTINEL")

    result = HandlerSetupBaseTools(collect=fail).handle(
        ModelSetupPlan(workspace=tmp_path, action="verify")
    )
    assert result.status == "failure"
    assert "SENTINEL" not in result.model_dump_json()
