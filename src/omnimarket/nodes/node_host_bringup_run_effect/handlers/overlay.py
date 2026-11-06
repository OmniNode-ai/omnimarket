# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The node overlay: ``<root>/node_host_bringup_run_effect/overlay.yaml``."""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_host_bringup_run_effect.models import (
    ModelHostBringupRunOverlay,
)

NODE_NAME = "node_host_bringup_run_effect"


def load_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelHostBringupRunOverlay:
    """Raises NodeOverlayError naming how to supply the overlay when no root holds one."""
    return load_structured_overlay(NODE_NAME, ModelHostBringupRunOverlay, environ)
