# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Public entry point for the start-path census and refusal drill."""

from omnimarket.nodes.node_start_path_census_compute.handlers.handler_start_path_census import (
    HandlerStartPathCensus,
)


class NodeStartPathCensusCompute(HandlerStartPathCensus):
    """Registered ONEX node."""


__all__ = ["NodeStartPathCensusCompute"]
