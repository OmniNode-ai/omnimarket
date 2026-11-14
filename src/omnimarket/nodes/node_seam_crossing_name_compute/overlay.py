# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolve the crossing name labels at composition, before the pure handler runs."""

from collections.abc import Mapping

from omnimarket.handlers.handler_packaged_contract_loader import (
    load_packaged_contract_section,
)
from omnimarket.handlers.node_overlay_reader import (
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.models.seam_crossing_name import (
    ModelSeamCrossingNaming,
    ModelSeamCrossingNamingConfig,
)

NODE_NAME = "node_seam_crossing_name_compute"


def load_seam_crossing_naming(
    environ: Mapping[str, str] | None = None,
) -> ModelSeamCrossingNaming:
    """Read the deployment's labels, or the contract's packaged ones.

    The optional overlay is ``<root>/<node_name>/overlay.yaml`` with the three label
    keys. An invalid selected overlay refuses composition; it never silently falls
    back to the packaged labels.
    """
    if find_node_overlay(NODE_NAME, environ) is not None:
        return load_structured_overlay(NODE_NAME, ModelSeamCrossingNaming, environ)
    return ModelSeamCrossingNamingConfig.model_validate(
        load_packaged_contract_section(NODE_NAME, "config")
    ).naming
