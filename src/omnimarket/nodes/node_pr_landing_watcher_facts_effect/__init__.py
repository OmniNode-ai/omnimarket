# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Landing watcher facts node: landing facts for the overlay's repositories, read from the PR watcher's state."""

from pathlib import Path

from omnimarket.nodes.node_pr_landing_watcher_facts_effect.handlers.handler_pr_landing_watcher_facts import (
    HandlerPrLandingWatcherFacts,
)


class NodePrLandingWatcherFactsEffect(HandlerPrLandingWatcherFacts):
    """ONEX entry-point wrapper for HandlerPrLandingWatcherFacts."""

    contract_path = Path(__file__).with_name("contract.yaml")


__all__ = ["HandlerPrLandingWatcherFacts", "NodePrLandingWatcherFactsEffect"]
