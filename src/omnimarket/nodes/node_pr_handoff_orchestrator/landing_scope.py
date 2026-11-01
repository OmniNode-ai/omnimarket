# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the landing scope overlay when composing the handoff orchestrator."""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.models.pr_handoff import (
    ModelPrHandoffLandingScopeOverlay,
    ModelPrHandoffLandingScopeReading,
)

LANDING_SCOPE_OVERLAY = "landing_scope"


def read_landing_scope(
    environ: Mapping[str, str] | None = None,
) -> ModelPrHandoffLandingScopeReading:
    """Return the overlay's scope, or the reader's value-free failure detail."""
    try:
        overlay = load_structured_overlay(
            LANDING_SCOPE_OVERLAY, ModelPrHandoffLandingScopeOverlay, environ
        )
    except NodeOverlayError as error:
        return ModelPrHandoffLandingScopeReading(problem=str(error))
    return ModelPrHandoffLandingScopeReading(repos=overlay.repos)


__all__: list[str] = ["LANDING_SCOPE_OVERLAY", "read_landing_scope"]
