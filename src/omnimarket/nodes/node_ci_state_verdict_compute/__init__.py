# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ci-watch verdict compute node (OMN-20686)."""

from omnimarket.nodes.node_ci_state_verdict_compute.handlers.handler_ci_state_verdict import (
    HandlerCiStateVerdict,
)


class NodeCiStateVerdictCompute(HandlerCiStateVerdict):
    """ONEX entrypoint for what ci-watch prints about a PR's CI."""


__all__ = ["HandlerCiStateVerdict", "NodeCiStateVerdictCompute"]
