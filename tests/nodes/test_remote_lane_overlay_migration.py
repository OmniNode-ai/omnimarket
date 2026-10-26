# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deployment inputs for the remote-lane nodes come from overlays."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_brief_prerequisites_compute import (
    ModelBriefPrerequisitesRequest,
    NodeBriefPrerequisitesCompute,
)
from omnimarket.nodes.node_install_canary_effect.handlers.handler_install_canary import (
    canary_unit_repos,
)
from omnimarket.nodes.node_lane_report_relay_effect.handlers.handler_lane_report_relay import (
    remote_script,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def overlays(tmp_path, monkeypatch):
    monkeypatch.delenv("ONEX_REGISTRY_ROOT", raising=False)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    docs = {
        "repos": {
            "private_data": "repo-private-data",
            "plugin_source": "repo-plugin-source",
            "knowledge_base": "repo-knowledge",
            "deploy": "repo-deploy",
            "private_data_home_envs": ["PRIVATE_DATA_ROOT"],
            "knowledge_base_home_env": "KNOWLEDGE_ROOT",
            "beside_root": ["private_data", "knowledge_base"],
        },
        "lab_hosts": {
            "hosts": [{"name": "host-a", "target": "192.0.2.1"}],
            "registry_root_candidates": ["$HOME/registry"],
        },
    }
    for name, doc in docs.items():
        directory = tmp_path / name
        directory.mkdir()
        (directory / "overlay.yaml").write_text(yaml.safe_dump(doc))
    return tmp_path


def test_canary_defaults_read_repository_roles(overlays):
    assert canary_unit_repos() == (
        "repo-private-data",
        "repo-plugin-source",
        "omniclaude",
    )


def test_missing_repository_overlay_refuses_with_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    with pytest.raises(NodeOverlayError, match=r"repos\.private_data"):
        canary_unit_repos()


def test_brief_accepts_the_public_registry_variable():
    result = NodeBriefPrerequisitesCompute().handle(
        ModelBriefPrerequisitesRequest(
            "Required host: host-a\nHost-local inputs: $ONEX_REGISTRY_ROOT/input.txt",
            None,
        )
    )
    assert result.host == "host-a"
    assert result.required
    assert result.inputs == ("$ONEX_REGISTRY_ROOT/input.txt",)


def test_relay_root_candidates_expand_on_the_remote_host(overlays, tmp_path):
    root = tmp_path / "remote" / "registry"
    reports = root / ".claude_scratch/reports"
    reports.mkdir(parents=True)
    name = "lane-a-2026-10-26T1735Z.md"
    (reports / name).write_text("report")
    proc = subprocess.run(
        ["bash", "-c", remote_script(None, "lane-a")],
        env={"HOME": str(root.parent), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        check=True,
    )
    assert proc.stdout.startswith(b"RELAY-TAR\n")
    assert name.encode() in proc.stdout


def test_missing_root_overlay_refuses_with_key(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    monkeypatch.delenv("ONEX_REGISTRY_ROOT", raising=False)
    with pytest.raises(NodeOverlayError, match=r"lab_hosts\.registry_root_candidates"):
        remote_script(None, "lane-a")


def test_explicit_remote_root_needs_no_overlay(tmp_path, monkeypatch):
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "empty"))
    script = remote_script(str(tmp_path), "lane-a")
    assert "lab_hosts" not in script
