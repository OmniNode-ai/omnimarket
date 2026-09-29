"""Handlers for the board probe-results projection (OMN-19937)."""

from omnimarket.nodes.node_projection_board_probe_results.handlers.board_probe_results_fold import (
    HandlerProjectionBoardProbeResults,
    satisfies_verdict_request,
)

__all__ = ["HandlerProjectionBoardProbeResults", "satisfies_verdict_request"]
