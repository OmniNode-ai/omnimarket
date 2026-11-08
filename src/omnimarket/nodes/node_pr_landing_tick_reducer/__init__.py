# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Landing tick reducer node: a tick's decision folded to a row per pull request."""

from pathlib import Path

from omnimarket.nodes.node_pr_landing_tick_reducer.handlers.handler_pr_landing_tick_reducer import (
    HandlerPrLandingTickReducer,
    fold_decision,
)


class NodePrLandingTickReducer(HandlerPrLandingTickReducer):
    """ONEX entry-point wrapper for HandlerPrLandingTickReducer."""

    contract_path = Path(__file__).with_name("contract.yaml")


__all__ = ["HandlerPrLandingTickReducer", "NodePrLandingTickReducer", "fold_decision"]
