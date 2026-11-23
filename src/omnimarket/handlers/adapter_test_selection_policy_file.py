# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Reads the selector's policy.yaml: the one place the node's settings enter."""

from __future__ import annotations

import hashlib
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
        policy = ModelSelectorPolicy.model_validate(yaml.safe_load(handle))
    policy.closure_settings()  # a policy file must set the closure keys: a snapshot alone may omit them
    return policy


CONTRACT_PATH = POLICY_PATH.with_name("contract.yaml")


def policy_version(path: Path = POLICY_PATH) -> str:
    """The version of the policy a selection was made under: the digest of the file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def selector_version(path: Path = CONTRACT_PATH) -> str:
    """The selector node's version, as its contract declares it."""
    with path.open() as handle:
        return str(yaml.safe_load(handle)["node_version"])
