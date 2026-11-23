# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds this node's handlers."""

from collections.abc import Callable

from omnimarket.nodes.node_owner_summary_effect.handlers import (
    HandlerOwnerSummary,
)


def build_handler_owner_summary(
    ledger_rows: Callable[[int], tuple[str, ...]] | None = None,
) -> HandlerOwnerSummary:
    """Build the handler; its rows come from the rows loader node unless ``ledger_rows`` answers them."""
    return HandlerOwnerSummary(ledger_rows)
