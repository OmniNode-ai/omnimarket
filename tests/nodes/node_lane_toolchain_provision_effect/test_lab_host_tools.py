# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay missing lab tools and shell commands whose final status masks failure."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from omnimarket.nodes.node_host_capability_compute.handlers.handler_lane_toolchain import (
    HandlerLaneToolchain,
)
from omnimarket.nodes.node_host_capability_compute.models import (
    ModelHostToolchainReading,
    ModelLaneToolchainRequest,
    ModelToolReading,
)
from omnimarket.nodes.node_lane_toolchain_provision_effect.handlers.adapter_host_files import (
    HostFiles,
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


def test_every_declared_lab_host_requires_the_complete_toolchain() -> None:
    table = load_table()
    required = {"onex", "node", "pnpm", "make", "jq", "python", "pyright"}
    for host, tools in table.hosts.items():
        assert required <= set(tools), host
        assert tools.index("node") < tools.index("pyright")


@pytest.mark.parametrize("tool", ["make", "jq", "python"])
def test_missing_build_and_script_tools_are_named_by_the_parity_read(tool: str) -> None:
    result = HandlerLaneToolchain().handle(
        ModelLaneToolchainRequest(
            table=load_table(),
            readings=(
                ModelHostToolchainReading(
                    host="h053",
                    tools=(ModelToolReading(tool=tool, status=127),),
                ),
            ),
        )
    )
    assert [(f.tool, f.state) for f in result.failures] == [(tool, "missing")]


@pytest.mark.parametrize(
    "tail", ["sh: pnpm: command not found", "/bin/sh: 1: pnpm: not found"]
)
def test_a_zero_exit_with_a_missing_command_is_not_a_working_tool(tail: str) -> None:
    table = load_table()

    class MaskedFailure(FakeHost):
        def run(self, argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
            if argv[0] == "pnpm":
                return 0, tail
            return 0, "working"

    # Use the committed tool definitions with the fixture's host deployment facts.
    fixture_table = table.model_copy(update={"hosts": {"lane-a.test": ("pnpm",)}})
    result = HandlerLaneToolchainProvision(
        overlay(), fixture_table, MaskedFailure()
    ).handle(ModelLaneToolchainProvisionRequest(host="lane-a.test"))
    assert [(s.step, s.state) for s in result.steps] == [
        ("tool:pnpm", "needs-operator")
    ]
    assert result.operator_steps
    parity = HandlerLaneToolchain().handle(
        ModelLaneToolchainRequest(
            table=fixture_table,
            readings=(
                ModelHostToolchainReading(
                    host="lane-a.test",
                    tools=(ModelToolReading(tool="pnpm", status=0, tail=tail),),
                ),
            ),
        )
    )
    assert [(f.tool, f.state) for f in parity.failures] == [("pnpm", "missing")]


def test_recorded_zero_status_can_be_reproduced_at_the_real_host_boundary(
    tmp_path: Path,
) -> None:
    # An empty PATH makes the missing command independent of the CI host's tools.
    status, tail = HostFiles().run(
        ("/bin/sh", "-c", "pnpm --version; true"), {"PATH": str(tmp_path)}
    )
    assert status == 0
    assert "pnpm:" in tail
    assert any(marker in tail for marker in ("command not found", ": not found"))


def test_a_version_probe_that_works_is_unchanged() -> None:
    table = load_table().model_copy(update={"hosts": {"lane-a.test": ("pnpm",)}})

    class WorkingHost(FakeHost):
        def run(self, argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
            return 0, "10.20.0"

    result = HandlerLaneToolchainProvision(overlay(), table, WorkingHost()).handle(
        ModelLaneToolchainProvisionRequest(host="lane-a.test")
    )
    assert [s.state for s in result.steps] == ["unchanged"]
    assert result.operator_steps == ()


@pytest.mark.parametrize("tool", ["make", "jq", "python"])
def test_effect_installs_missing_build_tool_and_reprobes(tool: str) -> None:
    table = load_table().model_copy(update={"hosts": {"lane-a.test": (tool,)}})

    class InstallableHost(FakeHost):
        installed = False

        def run(self, argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
            self.ran.append(tuple(argv))
            if tuple(argv) == ("install-tool", tool):
                self.installed = True
                return 0, "installed"
            return (0, "version 1") if self.installed else (127, "FileNotFoundError")

    port = InstallableHost()
    handler = HandlerLaneToolchainProvision(
        overlay({tool: ("install-tool", tool)}), table, port
    )
    request = ModelLaneToolchainProvisionRequest(host="lane-a.test")
    result = handler.handle(request)
    assert [s.state for s in result.steps] == ["changed"]
    assert port.ran == [
        table.tools[tool].probe,
        ("install-tool", tool),
        table.tools[tool].probe,
    ]
    port.ran.clear()
    assert [s.state for s in handler.handle(request).steps] == ["unchanged"]
    assert port.ran == [table.tools[tool].probe]
