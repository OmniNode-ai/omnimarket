# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden and error chain: node_host_installer_clone_sync_units installs and refuses through the node entry point."""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.node_host_installer_clone_sync_units.node import (
    NodeHostInstallerCloneSyncUnits,
)

from .test_host_clone_sync_units import Recorder, _request


def test_clone_sync_golden_chain_installs_the_systemd_units_through_the_node(
    tmp_path: Path,
) -> None:
    node = NodeHostInstallerCloneSyncUnits(Recorder())
    contract = yaml.safe_load(node.contract_path.read_text())
    assert contract["name"] == "node_host_installer_clone_sync_units"
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.host-clone-sync-unit-completed.v1"
    )
    assert contract["terminal_event"] in contract["event_bus"]["publish_topics"]

    result = node.handle(_request(tmp_path, "canonical-clone-sync-systemd", "install"))
    assert result.status == "success", result.stderr
    assert result.changed is True
    service = (
        tmp_path / "home/.config/systemd/user/onex-canonical-clone-sync.service"
    ).read_text()
    assert "StandardOutput=journal\n" in service


def test_clone_sync_error_chain_refuses_through_the_node_and_writes_nothing(
    tmp_path: Path,
) -> None:
    request = _request(tmp_path, "canonical-clone-sync-systemd", "install")
    (
        Path(request.registry_root)
        / "omniclaude/plugins/onex/hooks/lib/canonical_clone_sync.py"
    ).unlink()
    result = NodeHostInstallerCloneSyncUnits(Recorder()).handle(request)
    assert result.status == "refused"
    assert not (tmp_path / "home/.config").exists()
