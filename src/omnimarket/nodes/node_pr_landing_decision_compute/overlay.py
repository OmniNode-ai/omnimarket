# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve exhaustion accountability at composition, before the pure tick."""

from collections.abc import Mapping

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)
from omnimarket.handlers.node_overlay_reader import (
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_exhaustion_owner import (
    ModelLandingExhaustionConfig,
    ModelLandingExhaustionOwner,
)

NODE_NAME = "node_pr_landing_decision_compute"


def load_exhaustion_owner(
    environ: Mapping[str, str] | None = None,
) -> ModelLandingExhaustionOwner:
    """Read the deployment owner or the contract's accountable controller lane.

    The optional overlay is ``<root>/<node_name>/overlay.yaml`` with ``kind``
    and ``name``. An invalid selected overlay refuses composition; it never
    silently hands responsibility back to the default lane.
    """
    if find_node_overlay(NODE_NAME, environ) is not None:
        return load_structured_overlay(NODE_NAME, ModelLandingExhaustionOwner, environ)
    return ModelLandingExhaustionConfig.model_validate(
        load_packaged_contract_section(NODE_NAME, "config")
    ).exhaustion_owner
