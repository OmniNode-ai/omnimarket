# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handlers for node_capture_item_identity_effect.

The hooks load ``handler_item_identity`` by file path, not through this package
(whose import would run every package ``__init__`` on the way); see its docstring.
"""

from omnimarket.nodes.node_capture_item_identity_effect.handlers.handler_item_identity import (
    HandlerCaptureItemIdentity,
)

__all__ = ["HandlerCaptureItemIdentity"]
