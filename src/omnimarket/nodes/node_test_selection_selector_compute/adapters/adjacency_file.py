# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Loads the static module adjacency map from its YAML file."""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.node_test_selection_selector_compute.adapters.source_text_file import (
    SourceTextFile,
)
from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_path_resolution import (
    ModelAdjacencyMap,
)
from omnimarket.nodes.node_test_selection_selector_compute.protocols.protocol_source_text import (
    ProtocolSourceText,
)


def load_adjacency_map(
    path: Path, source: ProtocolSourceText | None = None
) -> ModelAdjacencyMap:
    raw = yaml.safe_load((source or SourceTextFile()).read_text(path))
    return ModelAdjacencyMap.model_validate(raw)
