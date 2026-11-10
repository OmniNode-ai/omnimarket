# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve the provision rule and overlay at composition, before the pure plan."""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)
from omnimarket.handlers.node_overlay_reader import (
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.nodes.node_topic_provision_plan_compute.models.model_topic_provision_overlay import (
    ModelTopicProvisionOverlay,
)
from omnimarket.nodes.node_topic_provision_plan_compute.models.model_topic_provision_rule import (
    ModelTopicProvisionConfig,
    ModelTopicProvisionRule,
)

NODE_NAME = "node_topic_provision_plan_compute"


def load_provision_rule(
    environ: Mapping[str, str] | None = None,
) -> ModelTopicProvisionRule:
    """Read the provision rule from the overlay or the contract's config section."""
    if find_node_overlay(NODE_NAME, environ) is not None:
        overlay = load_structured_overlay(
            NODE_NAME, ModelTopicProvisionOverlay, environ
        )
        if overlay.mode is not None:
            return ModelTopicProvisionRule(mode=overlay.mode)
    return ModelTopicProvisionConfig.model_validate(
        load_packaged_contract_section(NODE_NAME, "config")
    ).rule


def load_provision_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelTopicProvisionOverlay:
    """Read the full overlay (mode, profile, lane_manifest, contract_roots, bootstrap_env)."""
    if find_node_overlay(NODE_NAME, environ) is not None:
        return load_structured_overlay(NODE_NAME, ModelTopicProvisionOverlay, environ)
    return ModelTopicProvisionOverlay()
