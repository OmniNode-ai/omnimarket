# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node's contract, metadata and entry point agree with its handlers."""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
NODE = ROOT / "src/omnimarket/nodes/node_review_lock_effect"


def _load(module: str, name: str) -> type:
    cls = getattr(importlib.import_module(module), name)
    assert isinstance(cls, type)
    return cls


def test_contract_names_importable_handlers_and_models() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert contract["name"] == "node_review_lock_effect"
    for entry in (contract["handler"], *contract["additional_handlers"]):
        assert _load(entry["module"], entry["class"]).__name__ == entry["class"]
        module, _, name = entry["input_model"].rpartition(".")
        _load(module, name)
    for key in ("input_model", "output_model"):
        _load(contract[key]["module"], contract[key]["name"])
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}


def test_entry_point_is_registered_in_pyproject_and_metadata() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    points = pyproject["project"]["entry-points"]["onex.nodes"]
    assert (
        points["node_review_lock_effect"] == "omnimarket.nodes.node_review_lock_effect"
    )
    metadata = yaml.safe_load((NODE / "metadata.yaml").read_text())
    assert (
        metadata["entry_points"]["onex.nodes"]["node_review_lock_effect"]
        == points["node_review_lock_effect"]
    )
