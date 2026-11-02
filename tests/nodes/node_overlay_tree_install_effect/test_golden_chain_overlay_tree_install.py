# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: node_overlay_tree_install_effect installs a branch revision and verifies it, through the node entry point."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from omnibase_core.validators.no_unguarded_git_subprocess import scrub_git_location_env

from omnimarket.nodes.node_overlay_tree_install_effect.models.model_overlay_tree_install import (
    ModelOverlayTreeInstallRequest,
)
from omnimarket.nodes.node_overlay_tree_install_effect.node import (
    NodeOverlayTreeInstallEffect,
)


def _git(clone: Path, *args: str) -> str:
    done = subprocess.run(
        [
            "git",
            "-C",
            str(clone),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
        env=scrub_git_location_env(os.environ),
    )
    return done.stdout.strip()


def test_a_branch_revision_installs_then_verifies_against_its_recorded_commit(
    tmp_path: Path,
) -> None:
    clone = tmp_path / "clone"
    plugin = clone / "plugins" / "onex-overlays"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / "overlays" / "floor").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "onex-overlays"}), encoding="utf-8"
    )
    (plugin / "overlays" / "floor" / "overlay.yaml").write_text(
        "owner_login: someone\n", encoding="utf-8"
    )
    _git(clone, "init", "-q", "-b", "fix")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "overlay")
    tip = _git(clone, "rev-parse", "HEAD")
    node = NodeOverlayTreeInstallEffect()
    common = {
        "install_path": str(tmp_path / "cache" / "1.0.0"),
        "clone": str(clone),
        "record": str(tmp_path / "state" / "record.json"),
    }
    installed = node.handle(
        ModelOverlayTreeInstallRequest(operation="install", ref="fix", **common)
    )
    assert installed.verdict == "pass"
    assert installed.commit == tip
    verified = node.handle(ModelOverlayTreeInstallRequest(operation="verify", **common))
    assert verified.verdict == "pass"
    assert verified.commit == tip
