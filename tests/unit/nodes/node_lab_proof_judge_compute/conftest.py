# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared fixtures: the packaged example overlay, and a deployment overlay root the CLI reads it from."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_lab_proof_judge_compute.overlay import (
    NODE_NAME,
    ModelLabProofJudgeOverlay,
)

_EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_lab_proof_judge_compute/overlay.example.yaml"
)


@pytest.fixture(scope="session")
def overlay() -> ModelLabProofJudgeOverlay:
    """The packaged example overlay, loaded from the node's own file."""
    return ModelLabProofJudgeOverlay.model_validate(
        yaml.safe_load(_EXAMPLE.read_text())
    )


@pytest.fixture
def deployed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An overlay root holding the example overlay and a receipts root, both named through the environment."""
    root = tmp_path / "overlays"
    (root / NODE_NAME).mkdir(parents=True)
    shutil.copy(_EXAMPLE, root / NODE_NAME / "overlay.yaml")
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(root))
    monkeypatch.setenv("LAB_PROOF_JUDGE_RECEIPTS_ROOT", str(receipts))
    return receipts
