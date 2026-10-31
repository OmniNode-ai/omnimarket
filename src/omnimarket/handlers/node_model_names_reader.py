# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Composition adapter: the model names a node declares in ``configs/node_model_names.v1.yaml``.

The manifest lists model names by node. A node contract holds no model literal, so
the names sit in this one manifest. A missing manifest, node or malformed entry
refuses at composition; the package supplies no fallback name.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path

import yaml

from omnimarket.models.model_node_model_names import ModelNodeModelNames

NODE_MODEL_NAMES_YAML = (
    Path(__file__).resolve().parent.parent / "configs" / "node_model_names.v1.yaml"
)


@cache
def load_node_model_names(node_name: str) -> ModelNodeModelNames:
    """The model names ``node_name`` declares; raises when the manifest declares none."""
    document = yaml.safe_load(NODE_MODEL_NAMES_YAML.read_text(encoding="utf-8"))
    return ModelNodeModelNames.model_validate({"names": document["nodes"][node_name]})


__all__ = ["NODE_MODEL_NAMES_YAML", "load_node_model_names"]
