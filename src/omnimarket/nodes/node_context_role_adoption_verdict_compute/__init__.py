# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Deterministic TE.5 role adoption verdict compute node (OMN-21142)."""

from omnimarket.nodes.node_context_role_adoption_verdict_compute.handlers.handler_role_adoption_verdict import (
    HandlerRoleAdoptionVerdict,
)


class NodeContextRoleAdoptionVerdictCompute(HandlerRoleAdoptionVerdict):
    """ONEX entrypoint for adoption decisions over recorded round-one results."""


__all__ = ["HandlerRoleAdoptionVerdict", "NodeContextRoleAdoptionVerdictCompute"]
