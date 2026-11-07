# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_runner_group_exposure_compute: can a runner group be reached from a public origin."""

from omnimarket.nodes.node_runner_group_exposure_compute.handlers.handler_runner_group_exposure import (
    HandlerRunnerGroupExposure,
)

__all__ = ["HandlerRunnerGroupExposure", "NodeRunnerGroupExposureCompute"]


class NodeRunnerGroupExposureCompute(HandlerRunnerGroupExposure):
    """ONEX entry-point wrapper for HandlerRunnerGroupExposure."""
