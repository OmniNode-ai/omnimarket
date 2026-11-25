# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The declared repository retirement policy, read from the host reconcile contract and its overlay.

Shared so that any node that must skip a retired repository reads the one declared list rather than keeping its own.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import find_node_overlay
from omnimarket.models.model_repository_retirement import (
    ModelRepositoryRetirementOverlay,
    ModelRepositoryRetirementPolicy,
)

_POLICY_NODE = "node_host_reconcile_effect"


def load_retirement_policy() -> ModelRepositoryRetirementPolicy:
    contract = yaml.safe_load(
        (
            Path(__file__).resolve().parents[1]
            / "nodes"
            / _POLICY_NODE
            / "contract.yaml"
        ).read_text()
    )
    policy = ModelRepositoryRetirementPolicy.model_validate(contract["retirement"])
    path = find_node_overlay(_POLICY_NODE)
    if path is None:
        return policy
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("host reconcile overlay must be a mapping")
    overlay = ModelRepositoryRetirementOverlay.model_validate(
        document.get("retirement", {})
    )
    return policy.model_copy(
        update={
            "retired_repositories": tuple(
                dict.fromkeys(
                    (
                        *policy.retired_repositories,
                        *overlay.additional_retired_repositories,
                    )
                )
            )
        }
    )
