# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The crush tool the committed table declares for the hosts that run lab code drafts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import pytest

from omnimarket.nodes.node_lane_toolchain_provision_effect.handlers.adapter_host_files import (
    load_table,
)
from omnimarket.nodes.node_lane_toolchain_provision_effect.handlers.handler_lane_toolchain_provision import (
    HandlerLaneToolchainProvision,
)
from omnimarket.nodes.node_lane_toolchain_provision_effect.models import (
    ModelLaneToolchainProvisionRequest,
)
from tests.nodes.node_lane_toolchain_provision_effect.test_lane_toolchain_provision import (
    FakeHost,
    overlay,
)

pytestmark = pytest.mark.unit


def test_crush_is_declared_for_the_hosts_that_run_lab_drafts() -> None:
    table = load_table()
    assert tuple(table.tools["crush"].probe) == ("crush", "--version")
    assert table.tools["crush"].repair.kind == "install"
    for host in ("h051", "h101", "h201"):
        assert "crush" in table.hosts[host], host
        assert table.hosts[host].index("crush") < table.hosts[host].index("pyright"), (
            host
        )


def test_a_missing_crush_with_no_installer_is_an_operator_step() -> None:
    table = load_table().model_copy(update={"hosts": {"lane-a.test": ("crush",)}})

    class MissingCrush(FakeHost):
        def run(self, argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
            self.ran.append(tuple(argv))
            return 127, "FileNotFoundError"

    handler = HandlerLaneToolchainProvision(overlay(), table, MissingCrush())
    result = handler.handle(ModelLaneToolchainProvisionRequest(host="lane-a.test"))
    assert [(s.step, s.state) for s in result.steps] == [
        ("tool:crush", "needs-operator")
    ]
    assert result.operator_steps


def test_a_missing_crush_is_installed_by_the_overlay_argv_and_reprobed() -> None:
    table = load_table().model_copy(update={"hosts": {"lane-a.test": ("crush",)}})

    class InstallableCrush(FakeHost):
        installed = False

        def run(self, argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
            self.ran.append(tuple(argv))
            if tuple(argv) == ("install-tool", "crush"):
                self.installed = True
                return 0, "installed"
            return (0, "version 1") if self.installed else (127, "FileNotFoundError")

    port = InstallableCrush()
    handler = HandlerLaneToolchainProvision(
        overlay({"crush": ("install-tool", "crush")}), table, port
    )
    request = ModelLaneToolchainProvisionRequest(host="lane-a.test")
    result = handler.handle(request)
    assert [s.state for s in result.steps] == ["changed"]
    assert port.ran == [
        table.tools["crush"].probe,
        ("install-tool", "crush"),
        table.tools["crush"].probe,
    ]
    port.ran.clear()
    assert [s.state for s in handler.handle(request).steps] == ["unchanged"]
    assert port.ran == [table.tools["crush"].probe]


def test_a_failing_crush_installer_is_not_reported_changed() -> None:
    table = load_table().model_copy(update={"hosts": {"lane-a.test": ("crush",)}})

    class FailingCrushInstaller(FakeHost):
        def run(self, argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
            self.ran.append(tuple(argv))
            if tuple(argv) == ("install-tool", "crush"):
                return 1, "no network"
            return 127, "FileNotFoundError"

    port = FailingCrushInstaller()
    handler = HandlerLaneToolchainProvision(
        overlay({"crush": ("install-tool", "crush")}), table, port
    )
    result = handler.handle(ModelLaneToolchainProvisionRequest(host="lane-a.test"))
    assert len(result.steps) == 1
    assert result.steps[0].state == "failed"
    assert "no network" in result.steps[0].detail
