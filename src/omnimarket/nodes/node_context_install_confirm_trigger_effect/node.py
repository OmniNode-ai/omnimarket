# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-driven effect for install-confirm triggering (OMN-21115, TA.13).

The runtime wires the session reader and publishes result.confirmation to the contract's
completed topic when the outcome is request_ready.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnibase_core.nodes.node_effect import NodeEffect

if TYPE_CHECKING:
    from omnibase_core.models.container.model_onex_container import ModelONEXContainer


class NodeContextInstallConfirmTriggerEffect(NodeEffect):
    """Effect node: read captured compact content and prepare install confirmation."""

    def __init__(self, container: ModelONEXContainer) -> None:
        super().__init__(container)


__all__ = ["NodeContextInstallConfirmTriggerEffect"]
