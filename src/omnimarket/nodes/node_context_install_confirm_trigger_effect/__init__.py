# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Trigger install confirmation from projected session content (OMN-21115, TA.13).

The node publishes the install confirmation the compaction receipts projection folds.
"""

from omnimarket.nodes.node_context_install_confirm_trigger_effect.handlers.handler_context_install_confirm_trigger import (
    HandlerContextInstallConfirmTrigger,
)

__all__ = ["HandlerContextInstallConfirmTrigger"]
