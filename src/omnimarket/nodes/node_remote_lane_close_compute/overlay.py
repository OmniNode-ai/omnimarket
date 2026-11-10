# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve the needs= cell policy at composition, before the pure close decision (OMN-23417)."""

from collections.abc import Mapping

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)
from omnimarket.handlers.node_overlay_reader import (
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.models.remote_lane_close import ModelRemoteLaneNeedsPolicy

NODE_NAME = "node_remote_lane_close_compute"


def load_needs_policy(
    environ: Mapping[str, str] | None = None,
) -> ModelRemoteLaneNeedsPolicy:
    """Read the deployment overlay's policy, or the contract's declared defaults.

    The optional overlay is ``<root>/node_remote_lane_close_compute/overlay.yaml`` with every policy key.
    An invalid selected overlay refuses composition; it never silently falls back to the contract.
    """
    if find_node_overlay(NODE_NAME, environ) is not None:
        return load_structured_overlay(NODE_NAME, ModelRemoteLaneNeedsPolicy, environ)
    config = load_packaged_contract_section(NODE_NAME, "config")
    if not isinstance(config, dict):
        raise ValueError(f"{NODE_NAME}: contract config must be a mapping")
    return ModelRemoteLaneNeedsPolicy.model_validate(config["needs"])
