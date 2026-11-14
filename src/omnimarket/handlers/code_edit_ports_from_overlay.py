# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the code edit loop's ports from the node's ports overlay (OMN-24272).

When a runtime message, not ``onex code-edit run``, starts the loop, the
ports come from the node's ports overlay. An install with no overlay leaves
the loop without ports, and a runtime message that starts it is refused; an
overlay that is present but invalid raises, never a default.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayNotFoundError,
    load_structured_overlay,
)
from omnimarket.models.model_code_edit_ports_overlay import (
    CODE_EDIT_PORTS_OVERLAY_ID,
    ModelCodeEditPortsOverlay,
)

if TYPE_CHECKING:
    from omnimarket.delegated_code_edit.loop_ports import DelegatedCodeEditPorts


def load_code_edit_ports_overlay(
    environ: Mapping[str, str] | None = None,
) -> ModelCodeEditPortsOverlay | None:
    """The code edit ports overlay, or ``None`` when no overlay is configured.

    An install with no overlay root naming the ports overlay leaves the loop
    without ports. An overlay that is present but unreadable or invalid raises
    :class:`~omnimarket.handlers.node_overlay_reader.NodeOverlayError`, never a
    default.
    """
    try:
        return load_structured_overlay(
            CODE_EDIT_PORTS_OVERLAY_ID, ModelCodeEditPortsOverlay, environ
        )
    except NodeOverlayNotFoundError:
        return None


def build_code_edit_ports_from_overlay(
    environ: Mapping[str, str] | None = None,
) -> DelegatedCodeEditPorts | None:
    """The loop ports the overlay names, or ``None`` when no overlay is configured.

    The loop_ports import is lazy: it imports the node package, whose handler
    imports this module, so a top-level import would be circular.
    """
    from omnimarket.delegated_code_edit.loop_ports import (
        IN_PROCESS_DELEGATE_FLAGS,
        DelegatedCodeEditPorts,
        deployed_lane_delegate_flags,
    )

    overlay = load_code_edit_ports_overlay(environ)
    if overlay is None:
        return None
    return DelegatedCodeEditPorts(
        onex=Path(overlay.onex_path),
        state_root=Path(overlay.state_root),
        delegate_flags=(
            IN_PROCESS_DELEGATE_FLAGS
            if overlay.delegate_in_process
            else deployed_lane_delegate_flags(overlay.delegate_lane)
        ),
        max_tokens=overlay.max_tokens,
    )
