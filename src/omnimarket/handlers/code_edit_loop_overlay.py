# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Read the code edit loop overlay (OMN-23930).

The round limit, the check time limit and the empty-finish limit of the code
edit loop are values of the node's overlay. An install with no overlay keeps
the request model's own defaults; an overlay that is present but invalid
raises, never a default.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.models.model_code_edit_loop_overlay import (
    CODE_EDIT_LOOP_OVERLAY_ID,
    ModelCodeEditLoopOverlay,
)


def load_code_edit_loop_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelCodeEditLoopOverlay | None:
    """The code edit loop overlay, or ``None`` when no overlay is configured.

    An install with no overlay root naming the code edit loop overlay keeps the
    request model's own defaults. An overlay that is present but unreadable or
    invalid raises :class:`~omnimarket.handlers.node_overlay_reader.NodeOverlayError`,
    never a default.
    """
    try:
        return load_structured_overlay(
            CODE_EDIT_LOOP_OVERLAY_ID, ModelCodeEditLoopOverlay, environ
        )
    except NodeOverlayNotFoundError:
        return None
