# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the triage fence effect node's deployment overlay (OMN-20682).

The overlay file is ``<root>/node_triage_fences_effect/overlay.yaml`` under
``ONEX_SKILL_OVERLAY_ROOTS``. There is no packaged fallback value; a missing
overlay refuses with an error that names the setting.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay

from .models.model_triage_fences_overlay import ModelTriageFencesOverlay

NODE_NAME = "node_triage_fences_effect"


def load_triage_fences_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelTriageFencesOverlay:
    """Validate the node overlay; a missing overlay raises an error naming it."""
    return load_structured_overlay(NODE_NAME, ModelTriageFencesOverlay, environ)


__all__ = ["NODE_NAME", "load_triage_fences_overlay"]
