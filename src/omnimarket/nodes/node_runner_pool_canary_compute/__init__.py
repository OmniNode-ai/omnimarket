# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Runner-pool canary compute (OMN-22945)."""

from omnimarket.nodes.node_runner_pool_canary_compute.handlers.handler_runner_pool_canary import (
    HandlerRunnerPoolCanary,
)


class NodeRunnerPoolCanaryCompute(HandlerRunnerPoolCanary):
    """ONEX entrypoint for the runner-pool canary verdict."""


__all__ = ["HandlerRunnerPoolCanary", "NodeRunnerPoolCanaryCompute"]
