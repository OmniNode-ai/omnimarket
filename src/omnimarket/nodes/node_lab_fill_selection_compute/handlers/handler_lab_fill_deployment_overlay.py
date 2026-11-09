# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Load the lab-fill selection's deployment from its overlay (OMN-23312)."""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

from ..models.model_lab_fill_deployment import ModelLabFillDeployment
from ..models.model_lab_fill_selection_overlay import ModelLabFillSelectionOverlay

OVERLAY_ID = "node_lab_fill_selection_compute"


def load_lab_fill_deployment(
    environ: Mapping[str, str] | None = None,
) -> ModelLabFillDeployment:
    """The deployment the first overlay root holding the node's overlay declares; none refuses by name."""
    return load_structured_overlay(
        OVERLAY_ID, ModelLabFillSelectionOverlay, environ
    ).deployment()
