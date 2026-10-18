# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Client verdict table for the delegation adapter (OMN-21098)."""

from omnimarket.nodes.node_delegation_client_verdict_compute.handlers.handler_delegation_client_verdict import (
    HandlerDelegationClientVerdict,
)


class NodeDelegationClientVerdictCompute(HandlerDelegationClientVerdict):
    """ONEX entrypoint for the Claude Code adapter's delegation verdict."""


__all__ = ["HandlerDelegationClientVerdict", "NodeDelegationClientVerdictCompute"]
