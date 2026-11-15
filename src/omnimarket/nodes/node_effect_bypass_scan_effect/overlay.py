# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scan policy of node_effect_bypass_scan_effect: the packaged contract's ``config.scan``, or an overlay that replaces it.

Follows node_pr_landing_decision_compute/overlay.py: the contract carries the declared default; an overlay at
``<root>/node_effect_bypass_scan_effect/overlay.yaml`` under a root in ``ONEX_SKILL_OVERLAY_ROOTS`` replaces the whole policy. An invalid
selected overlay refuses; it never falls back to the contract's policy.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)
from omnimarket.handlers.node_overlay_reader import (
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.nodes.node_effect_bypass_scan_effect.models import (
    ModelBypassScanPolicy,
)

NODE_NAME = "node_effect_bypass_scan_effect"


def load_policy(environ: Mapping[str, str] | None = None) -> ModelBypassScanPolicy:
    """Read the overlay's policy when one is selected, else the contract's."""
    if find_node_overlay(NODE_NAME, environ) is not None:
        return load_structured_overlay(NODE_NAME, ModelBypassScanPolicy, environ)
    section = load_packaged_contract_section(NODE_NAME, "config")
    if not isinstance(section, dict) or "scan" not in section:
        raise ValueError(f"{NODE_NAME}: contract has no config.scan section")
    return ModelBypassScanPolicy.model_validate(section["scan"])
