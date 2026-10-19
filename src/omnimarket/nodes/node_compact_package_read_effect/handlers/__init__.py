# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Handlers for node_compact_package_read_effect.

The onex plugin's content capture loads ``handler_compact_package_read`` by file
path, not through this package (whose import would run every package
``__init__`` on the way); see its docstring.
"""

from omnimarket.nodes.node_compact_package_read_effect.handlers.handler_compact_package_read import (
    HandlerCompactPackageRead,
)

__all__ = ["HandlerCompactPackageRead"]
