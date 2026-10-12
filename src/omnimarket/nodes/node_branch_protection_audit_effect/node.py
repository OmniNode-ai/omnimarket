# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative effect node auditing branch protection; the contract routes to the handlers."""

from __future__ import annotations

from omnibase_core.nodes.node_effect import NodeEffect


class NodeBranchProtectionAuditEffect(NodeEffect):
    """Thin shell: the handlers bound in contract.yaml own the fetch and the audit."""


__all__ = ["NodeBranchProtectionAuditEffect"]
