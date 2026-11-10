# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain: node_host_installer_serve_units installs and refuses through the node entry point."""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.node_host_installer_serve_units.node import (
    NodeHostInstallerServeUnits,
)

from .test_host_serve_units import ROOTS_ENV, SERVE_ENV, Host


def test_serve_golden_chain_installs_the_units_through_the_node(tmp_path: Path) -> None:
    host = Host(tmp_path)
    node = NodeHostInstallerServeUnits(**host.wiring())
    contract = yaml.safe_load(node.contract_path.read_text())
    assert contract["name"] == "node_host_installer_serve_units"
    assert (
        contract["terminal_event"] == "onex.evt.omnimarket.host-serve-unit-completed.v1"
    )
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]

    result = node.handle(host.request(mode="install"))
    assert result.status == "success", result.stderr
    assert result.changed is True
    env = (host.home / ".config/app" / SERVE_ENV).read_text()
    assert f"{ROOTS_ENV}=" in env


def test_serve_error_chain_refuses_through_the_node_and_writes_nothing(
    tmp_path: Path,
) -> None:
    host = Host(tmp_path, linger="no")
    node = NodeHostInstallerServeUnits(**host.wiring())
    result = node.handle(host.request(mode="install"))
    assert result.status == "refused"
    assert (result.reason, result.exit_code) == ("linger-off", 69)
    assert not (host.home / ".config").exists()
