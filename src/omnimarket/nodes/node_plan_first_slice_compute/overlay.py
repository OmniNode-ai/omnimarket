# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve the first-slice check's settings at composition, before the pure check."""

from collections.abc import Mapping

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)
from omnimarket.handlers.node_overlay_reader import (
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.nodes.node_plan_first_slice_compute.models.model_plan_first_slice import (
    ModelPlanFirstSliceSettings,
)

NODE_NAME = "node_plan_first_slice_compute"


def load_settings(
    environ: Mapping[str, str] | None = None,
) -> ModelPlanFirstSliceSettings:
    """Read the deployment overlay or the contract's packaged ``config:`` section.

    The optional overlay is ``<root>/<node_name>/overlay.yaml`` with the three
    settings (``first_slice_headings``, ``min_section_words``,
    ``later_rung_headings``). An invalid selected overlay refuses composition;
    it never silently hands responsibility back to the packaged default.
    """
    if find_node_overlay(NODE_NAME, environ) is not None:
        return load_structured_overlay(NODE_NAME, ModelPlanFirstSliceSettings, environ)
    return ModelPlanFirstSliceSettings.model_validate(
        load_packaged_contract_section(NODE_NAME, "config")
    )
