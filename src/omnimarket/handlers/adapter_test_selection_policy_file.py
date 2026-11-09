# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reads the selector's policy.yaml: the one place the node's settings enter."""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.nodes.node_test_selection_selector_compute.models.model_test_selection_selector import (
    ModelSelectorPolicy,
)

POLICY_PATH = (
    Path(__file__).resolve().parents[1]
    / "nodes"
    / "node_test_selection_selector_compute"
    / "policy.yaml"
)


def load_policy(path: Path = POLICY_PATH) -> ModelSelectorPolicy:
    with path.open() as handle:
        return ModelSelectorPolicy.model_validate(yaml.safe_load(handle))
