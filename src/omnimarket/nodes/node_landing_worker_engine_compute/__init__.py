# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_landing_worker_engine_compute — pure compute policy (OMN-17427)."""

from omnimarket.nodes.node_landing_worker_engine_compute.handlers.handler_landing_worker_engine import (
    HandlerLandingWorkerEngine,
)


class NodeLandingWorkerEngineCompute(HandlerLandingWorkerEngine):
    """ONEX entry-point wrapper for HandlerLandingWorkerEngine."""


__all__ = ["HandlerLandingWorkerEngine", "NodeLandingWorkerEngineCompute"]
