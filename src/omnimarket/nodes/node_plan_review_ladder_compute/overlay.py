# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve the review ladder's settings at composition, before the pure decision."""

from collections.abc import Mapping

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)
from omnimarket.handlers.node_overlay_reader import (
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.nodes.node_plan_review_ladder_compute.models.model_plan_review_ladder import (
    ModelPlanReviewLadderSettings,
)

NODE_NAME = "node_plan_review_ladder_compute"


def load_settings(
    environ: Mapping[str, str] | None = None,
) -> ModelPlanReviewLadderSettings:
    """Read the deployment overlay or the contract's packaged ``config:`` section.

    The optional overlay is ``<root>/<node_name>/overlay.yaml`` with the
    blocking kinds (``blocking_kinds``). An invalid selected overlay refuses
    composition; it never silently hands responsibility back to the packaged
    default.
    """
    if find_node_overlay(NODE_NAME, environ) is not None:
        return load_structured_overlay(
            NODE_NAME, ModelPlanReviewLadderSettings, environ
        )
    return ModelPlanReviewLadderSettings.model_validate(
        load_packaged_contract_section(NODE_NAME, "config")
    )
