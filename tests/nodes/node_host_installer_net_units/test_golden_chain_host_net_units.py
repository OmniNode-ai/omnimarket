# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: node_host_installer_net_units installs a declared drop-in through the node entry point."""

from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from omnimarket.nodes.node_host_installer_net_units.handlers.handler_net_units import (
    HandlerNetUnits,
)
from omnimarket.nodes.node_host_installer_net_units.models.model_net_units import (
    ModelNetUnitsRequest,
)
from omnimarket.nodes.node_host_installer_net_units.node import (
    NodeHostInstallerNetUnits,
)

DROPIN = "ClientAliveInterval 60\n"


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(argv, 0, "active\n", "")


def test_host_net_golden_chain_installs_a_dropin_through_the_node(
    tmp_path: Path,
) -> None:
    overlay = tmp_path / "overlay"
    overlay.mkdir()
    (overlay / "alive.conf").write_text(DROPIN)
    (overlay / "units.yaml").write_text(
        yaml.safe_dump(
            {
                "schema": "host_net_units.v1",
                "units": {
                    "alive": {
                        "kind": "sshd-dropin",
                        "platform": "linux",
                        "hosts": ["pool-a"],
                        "files": [
                            {
                                "source": "alive.conf",
                                "destination": "/etc/ssh/sshd_config.d/20-alive.conf",
                                "mode": "0644",
                            }
                        ],
                    }
                },
            }
        )
    )
    node = NodeHostInstallerNetUnits()
    node.handler = HandlerNetUnits(
        run=_run, system=lambda: "Linux", sudo=[], fs_root=tmp_path / "fs"
    )
    contract = yaml.safe_load(node.contract_path.read_text())
    assert contract["name"] == "node_host_installer_net_units"

    request = ModelNetUnitsRequest(
        unit="alive", mode="install", host_name="pool-a", overlay_root=str(overlay)
    )
    result = node.handle(request)
    assert result.status == "success", result.stderr
    assert result.changed is True
    installed = tmp_path / "fs/etc/ssh/sshd_config.d/20-alive.conf"
    assert installed.read_text() == DROPIN

    again = node.handle(request)
    assert again.changed is False
    assert "no change" in again.stdout
