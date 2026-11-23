# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: node_host_installer_overlay_env_probe, through its entry point and its JSON entry, rows and no value."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_host_installer_overlay_env_probe import __main__ as entry
from omnimarket.nodes.node_host_installer_overlay_env_probe.models.model_overlay_env_probe import (
    ModelOverlayEnvProbeRequest,
    ModelProbeHost,
)
from omnimarket.nodes.node_host_installer_overlay_env_probe.node import (
    NodeHostInstallerOverlayEnvProbe,
)

NODE = "node_fixture_chain_effect"
NAME = "ONEX_DEV_READINESS_HOST"


def _roots(tmp_path: Path) -> tuple[str, ...]:
    (tmp_path / NODE).mkdir()
    (tmp_path / NODE / "overlay.yaml").write_text(
        yaml.safe_dump({"ready": {"host_env": NAME}})
    )
    return (str(tmp_path),)


def test_probe_golden_chain_rows_through_the_node(tmp_path: Path) -> None:
    request = ModelOverlayEnvProbeRequest(
        overlay_roots=_roots(tmp_path),
        hosts=(
            ModelProbeHost(host_name="has", nodes=(NODE,), set_names=(NAME,)),
            ModelProbeHost(host_name="lacks", nodes=(NODE,)),
        ),
    )
    result = NodeHostInstallerOverlayEnvProbe().handle(request)
    assert [(r.host_name, r.state) for r in result.rows] == [
        ("has", "set"),
        ("lacks", "unset"),
    ]
    assert [a.host_name for a in result.alarms] == ["lacks"]


def test_probe_golden_chain_json_entry_exit_code_and_no_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    request = ModelOverlayEnvProbeRequest(
        overlay_roots=_roots(tmp_path),
        hosts=(ModelProbeHost(host_name="lacks", nodes=(NODE,)),),
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(request.model_dump_json()))
    assert entry.main() == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed["rows"][0]["state"] == "unset"
    assert printed["rows"][0]["name"] == NAME
