# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load and validate the static module adjacency map."""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_path_resolution import (
    ModelAdjacencyEntry,
    ModelAdjacencyMap,
    ModelPathTriggeredTests,
    ModelThresholds,
)

__all__ = [
    "ModelAdjacencyEntry",
    "ModelAdjacencyMap",
    "ModelPathTriggeredTests",
    "ModelThresholds",
    "load_adjacency_map",
]


def load_adjacency_map(path: Path) -> ModelAdjacencyMap:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return ModelAdjacencyMap.model_validate(raw)
