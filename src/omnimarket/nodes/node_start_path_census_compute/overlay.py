# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``start_path_census`` overlay: which paths start an agent, session or provisioning call, none in code.

Shape only; the path list belongs to the deployment owner and is read from
``<root>/start_path_census/overlay.yaml``.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_start_path_census_compute.models.model_start_path_census import (
    ModelStartPathCensusPolicy,
)

OVERLAY_ID = "start_path_census"


def load_start_path_census_policy(
    environ: Mapping[str, str] | None = None,
) -> ModelStartPathCensusPolicy:
    """Load and validate the overlay; a missing key or a bad value is refused naming it."""
    return load_structured_overlay(OVERLAY_ID, ModelStartPathCensusPolicy, environ)
