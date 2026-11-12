# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared vocabulary for GitHub landing transport errors (OMN-23854).

Held here, below both the merge-sweep compute node and the GitHub HTTP
adapter, so callers can catch a typed transport failure without importing
the node's protocol module.
"""
