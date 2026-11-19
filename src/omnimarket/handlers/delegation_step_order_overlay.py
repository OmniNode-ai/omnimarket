# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the delegation step order overlay (OMN-24507).

The code engine order of a delegation step and the program each engine needs
on the host are values of the node's overlay. An install with no overlay
keeps the step's old default; an overlay that is present but invalid raises,
never a default.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.models.model_delegation_step_order_overlay import (
    DELEGATION_STEP_ORDER_OVERLAY_ID,
    ModelDelegationStepOrderOverlay,
)


def load_delegation_step_order_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelDelegationStepOrderOverlay | None:
    """The delegation step order overlay, or ``None`` when no overlay is configured.

    An install with no overlay root naming the delegation step order overlay
    keeps the step's old default. An overlay that is present but unreadable or
    invalid raises :class:`~omnimarket.handlers.node_overlay_reader.NodeOverlayError`,
    never a default.
    """
    try:
        return load_structured_overlay(
            DELEGATION_STEP_ORDER_OVERLAY_ID, ModelDelegationStepOrderOverlay, environ
        )
    except NodeOverlayNotFoundError:
        return None
