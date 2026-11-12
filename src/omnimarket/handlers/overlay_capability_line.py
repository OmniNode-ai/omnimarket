# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the capability map's class line from the deployment overlay (OMN-23628)."""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.models.delegation_capability_map.model_capability_line_reading import (
    ModelCapabilityLineReading,
)
from omnimarket.models.delegation_capability_map.model_capability_map_overlay import (
    ModelCapabilityMapOverlay,
)

OVERLAY_NODE = "node_projection_delegation_capability_map"
KEY = "delegation.capability_map.line"


def resolve_capability_line(
    environ: Mapping[str, str] | None = None,
) -> ModelCapabilityLineReading:
    """The class line from the deployment overlay, or why nothing configured it.

    The line has no packaged value: it is read from the overlay file
    ``<root>/node_projection_delegation_capability_map/overlay.yaml`` under a root
    named in ``ONEX_SKILL_OVERLAY_ROOTS``. When no overlay supplies the key
    ``delegation.capability_map.line``, the read carries the line None with a
    reason naming the key, rather than refusing. A selected overlay that does
    not validate still raises: a broken overlay must not read as an absent one.
    """
    try:
        overlay = load_structured_overlay(
            OVERLAY_NODE,
            ModelCapabilityMapOverlay,
            environ,
            missing_message=(
                f"{OVERLAY_NODE}: the overlay key {KEY} is not configured; supply an "
                f"overlay with field `{KEY}` at "
                f"<root>/{OVERLAY_NODE}/overlay.yaml under a root named in "
                f"ONEX_SKILL_OVERLAY_ROOTS"
            ),
        )
    except NodeOverlayNotFoundError as exc:
        return ModelCapabilityLineReading(not_configured_reason=str(exc))
    return ModelCapabilityLineReading(line=overlay.line)
