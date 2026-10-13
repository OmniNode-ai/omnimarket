# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Declarative local effect shell; contract.yaml routes to the git read handler."""

from omnibase_core.nodes.node_effect import NodeEffect


class NodeTicketContractPrerequisiteEffect(NodeEffect):
    """The contract-bound handler owns reading and checking; this shell owns no I/O."""


__all__ = ["NodeTicketContractPrerequisiteEffect"]
