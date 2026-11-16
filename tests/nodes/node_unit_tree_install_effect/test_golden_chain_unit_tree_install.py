# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: through the node entry point a request installs a commit and verifies it against the recorded revision; the example overlay validates."""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.node_unit_tree_install_effect import node as node_module
from omnimarket.nodes.node_unit_tree_install_effect.node import (
    NodeUnitTreeInstallEffect,
)
from omnimarket.nodes.node_unit_tree_install_effect.overlay import (
    ModelUnitTreeInstallOverlay,
)
from tests.nodes.node_unit_tree_install_effect.unit_tree_support import (
    environ,
    git,
    make_overlay,
    make_root,
    request,
)


def test_a_commit_installs_then_verifies_against_its_recorded_revision(
    tmp_path: Path,
) -> None:
    root = make_root(tmp_path)
    tip = git(root / "clone", "rev-parse", "HEAD")
    node = NodeUnitTreeInstallEffect(make_overlay(), environ(root))
    installed = node.handle(request())
    assert installed.verdict == "pass"
    assert installed.commit == tip
    verified = node.handle(request("verify"))
    assert verified.verdict == "pass"
    assert verified.commit == tip


def test_the_shipped_example_overlay_validates_against_the_model() -> None:
    path = Path(node_module.__file__).with_name("overlay.example.yaml")
    overlay = ModelUnitTreeInstallOverlay.model_validate(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )
    assert overlay.units[0].preserve_paths == (".venv",)
