# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor's unresolved-ledger step: the rows older than the window that nothing resolved (OMN-19998).

The handler and the runtime live in their own submodules; importing either here would loop back through the floor's
``source_loader``, which this node's handler imports, so this package exports nothing but its docstring.
"""

__all__ = []
