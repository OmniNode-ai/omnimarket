# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load the compose project census policy overlay."""

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import (
    find_node_overlay,
    load_structured_overlay,
)
from omnimarket.nodes.node_compose_project_census_compute.models.model_compose_project_census import (
    ModelComposeCensusPolicy,
)

NODE_NAME = "node_compose_project_census_compute"


def load_policy(environ: Mapping[str, str] | None = None) -> ModelComposeCensusPolicy:
    """Load the census policy overlay for this node."""
    if find_node_overlay(NODE_NAME, environ) is not None:
        return load_structured_overlay(NODE_NAME, ModelComposeCensusPolicy, environ)
    raise ValueError(f"no overlay found for {NODE_NAME!r}")
