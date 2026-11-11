# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Tests for the overlay model: example loads, missing keys refuse, duplicate node refuses."""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from omnimarket.nodes.node_lab_proof_judge_compute.overlay import (
    ModelLabProofJudgeOverlay,
    load_example_overlay,
)

_OVERLAY_EXAMPLE = (
    Path(__file__).resolve().parents[4]
    / "src/omnimarket/nodes/node_lab_proof_judge_compute/overlay.example.yaml"
)


def _load_example() -> dict:
    return yaml.safe_load(_OVERLAY_EXAMPLE.read_text())


def test_example_overlay_loads() -> None:
    """The example overlay loads successfully."""
    overlay = load_example_overlay()
    assert overlay.schema_version == 1
    assert overlay.not_applicable.class_id == "runs_outside_runtime"


@pytest.mark.parametrize(
    "missing_key",
    [
        "schema_version",
        "not_applicable",
        "substitute_receipts",
        "test_markers",
        "all_not_run_ok",
        "retention",
    ],
)
def test_missing_required_key_refuses(missing_key: str) -> None:
    """Each required key missing refuses."""
    data = _load_example()
    del data[missing_key]
    with pytest.raises(ValidationError):
        ModelLabProofJudgeOverlay.model_validate(data)


def test_duplicate_node_refused() -> None:
    """Duplicate node in entries is refused."""
    data = _load_example()
    repos = data["not_applicable"]["repos"]
    entry = copy.deepcopy(repos[0]["entries"][0])
    repos[0]["entries"].append(entry)
    with pytest.raises(Exception, match="duplicate"):
        ModelLabProofJudgeOverlay.model_validate(data)
