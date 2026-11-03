# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22414: the declared installer repairs a half-installed lane toolchain and re-probes before it reports a tool changed."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

from omnimarket.nodes.node_lane_toolchain_provision_effect.handlers.adapter_host_files import (
    HostFiles,
    load_table,
)
from omnimarket.nodes.node_lane_toolchain_provision_effect.handlers.handler_lane_toolchain_provision import (
    HandlerLaneToolchainProvision,
)
from omnimarket.nodes.node_lane_toolchain_provision_effect.models import (
    ModelLaneToolchainProvisionHost,
    ModelLaneToolchainProvisionOverlay,
    ModelLaneToolchainProvisionRequest,
    ModelLaneToolchainProvisionResult,
)

pytestmark = pytest.mark.unit

HOME = "/h/lane"
BIN = f"{HOME}/bin"
ROOT = f"{HOME}/reg"
WRAPPER = f"{ROOT}/omnibase_infra/scripts/onex"
NODEENV = f"{HOME}/.cache/pyright-python/nodeenv"
NODE_INSTALL: dict[str, tuple[str, ...]] = {"node": ("brew", "install", "node")}

TABLE_TEXT = """
tools:
  onex:
    probe: [onex, --help]
    repair: {kind: lane-link, target: omnibase_infra/scripts/onex}
    owner_fix: link the wrapper
  node:
    probe: [node, --version]
    repair: {kind: install}
    owner_fix: install node
  pyright:
    probe: [uvx, pyright, --version]
    repair: {kind: clear-empty-tree, path: .cache/pyright-python/nodeenv}
    owner_fix: fix the nodeenv
hosts:
  lane-a.test: [onex, node, pyright]
"""


class FakeHost:
    """A host whose tools work according to the files and links it holds."""

    def __init__(self, *, install_works: bool = True) -> None:
        self.links: dict[str, str] = {}
        self.dirs: dict[str, list[str]] = {NODEENV: []}
        self.files: set[str] = {WRAPPER}
        self.node_installed = False
        self.install_works = install_works
        self.ran: list[tuple[str, ...]] = []

    def exists(self, path: str) -> bool:
        return path in self.files or path in self.links or path in self.dirs

    def link_target(self, path: str) -> str | None:
        return self.links.get(path)

    def replace_link(self, path: str, target: str) -> None:
        self.links[path] = target

    def file_count(self, path: str) -> int | None:
        entries = self.dirs.get(path)
        return None if entries is None else len(entries)

    def remove_empty_tree(self, path: str) -> None:
        del self.dirs[path]

    def run(self, argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
        self.ran.append(tuple(argv))
        if argv[0] == "onex":
            if self.links.get(f"{BIN}/onex") == WRAPPER:
                return 0, "Usage: onex"
            return 1, "ModuleNotFoundError: No module named 'omnibase_core'"
        if argv[0] == "node":
            return (0, "v22") if self.node_installed else (127, "FileNotFoundError")
        if argv[0] == "uvx":
            if NODEENV in self.dirs and not self.dirs[NODEENV]:
                return 1, "nodeenv: exit 1"
            return 0, "pyright 1.1.391"
        if argv[0] == "brew":
            self.node_installed = self.install_works
            return (0, "") if self.install_works else (1, "brew: no network")
        return 127, "FileNotFoundError"


def overlay(
    install: dict[str, tuple[str, ...]] | None = None,
) -> ModelLaneToolchainProvisionOverlay:
    return ModelLaneToolchainProvisionOverlay(
        hosts=(
            ModelLaneToolchainProvisionHost(
                name="lane-a.test",
                home=HOME,
                lane_path=f"{BIN}:/usr/bin",
                lane_bin_dir=BIN,
                registry_root=ROOT,
                install=install or {},
            ),
        )
    )


def run(
    port: FakeHost,
    *,
    install: dict[str, tuple[str, ...]] | None = None,
    check_only: bool = False,
    host: str = "lane-a.test",
) -> ModelLaneToolchainProvisionResult:
    return HandlerLaneToolchainProvision(
        overlay(install), load_table(TABLE_TEXT), port
    ).handle(ModelLaneToolchainProvisionRequest(host=host, check_only=check_only))


def states(result: ModelLaneToolchainProvisionResult) -> dict[str, str]:
    return {s.step: s.state for s in result.steps}


def test_provisions_the_wrapper_link_the_node_install_and_clears_the_empty_nodeenv() -> (
    None
):
    port = FakeHost()
    result = run(port, install=NODE_INSTALL)
    assert states(result) == {
        "tool:onex": "changed",
        "tool:node": "changed",
        "tool:pyright": "changed",
    }
    assert port.links == {f"{BIN}/onex": WRAPPER}
    assert NODEENV not in port.dirs
    assert result.ok
    assert result.operator_steps == ()


def test_a_second_run_changes_nothing() -> None:
    port = FakeHost()
    install = NODE_INSTALL
    run(port, install=install)
    port.ran.clear()
    again = run(port, install=install)
    assert set(states(again).values()) == {"unchanged"}
    assert ("brew", "install", "node") not in port.ran


def test_check_only_names_each_change_and_writes_nothing() -> None:
    port = FakeHost()
    result = run(port, install=NODE_INSTALL, check_only=True)
    assert set(states(result).values()) == {"would-change"}
    assert port.links == {}
    assert NODEENV in port.dirs
    assert ("brew", "install", "node") not in port.ran


def test_a_tool_with_no_declared_installer_is_an_owner_step_not_a_failure() -> None:
    result = run(FakeHost())
    assert states(result)["tool:node"] == "needs-operator"
    assert result.ok
    assert any("node" in s and "install node" in s for s in result.operator_steps)


def test_an_install_that_fails_is_a_failure_with_its_output() -> None:
    result = run(FakeHost(install_works=False), install=NODE_INSTALL)
    step = next(s for s in result.steps if s.step == "tool:node")
    assert step.state == "failed"
    assert "brew: no network" in step.detail
    assert not result.ok


class NodeenvStaysBroken(FakeHost):
    """A host where clearing the directory does not mend pyright."""

    def run(self, argv: Sequence[str], env: Mapping[str, str]) -> tuple[int, str]:
        if argv[0] == "uvx":
            return 1, "nodeenv: exit 1"
        return super().run(argv, env)


def test_a_repair_whose_probe_still_fails_is_not_reported_changed() -> None:
    result = run(NodeenvStaysBroken(), install=NODE_INSTALL)
    assert states(result)["tool:pyright"] == "failed"


def test_a_real_file_where_the_link_goes_is_left_alone() -> None:
    port = FakeHost()
    port.files.add(f"{BIN}/onex")
    result = run(port)
    assert states(result)["tool:onex"] == "refused"
    assert f"{BIN}/onex" not in port.links


def test_an_undeclared_host_is_refused() -> None:
    result = run(FakeHost(), host="nowhere.test")
    assert [s.state for s in result.steps] == ["refused"]
    assert not result.ok


def test_a_nodeenv_that_holds_a_file_is_an_owner_step_and_is_not_removed() -> None:
    port = NodeenvStaysBroken()
    port.dirs[NODEENV] = ["bin/node"]
    result = run(port, install=NODE_INSTALL)
    assert states(result)["tool:pyright"] == "needs-operator"
    assert NODEENV in port.dirs


def test_the_real_host_clears_a_tree_of_empty_directories_and_keeps_one_with_a_file(
    tmp_path: Path,
) -> None:
    # The h053 reading of 2026-11-03: nodeenv/ held only an empty src/ directory and no bin/.
    host = HostFiles()
    stale = tmp_path / "nodeenv"
    (stale / "src").mkdir(parents=True)
    assert host.file_count(str(stale)) == 0
    host.remove_empty_tree(str(stale))
    assert not stale.exists()
    assert host.file_count(str(stale)) is None
    kept = tmp_path / "kept"
    (kept / "bin").mkdir(parents=True)
    (kept / "bin" / "node").write_text("x")
    assert host.file_count(str(kept)) == 1
    with pytest.raises(OSError, match="holds a file"):
        host.remove_empty_tree(str(kept))
    assert (kept / "bin" / "node").exists()
