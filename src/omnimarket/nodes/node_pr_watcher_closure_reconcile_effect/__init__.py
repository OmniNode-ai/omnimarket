# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""PR watcher closure reconciler effect node."""

from omnimarket.nodes.node_pr_watcher_closure_reconcile_effect.handlers.handler_pr_watcher_closure_reconcile import (
    HandlerPrWatcherClosureObservation,
    HandlerPrWatcherClosureReconcile,
    HandlerPrWatcherClosureTick,
)


class NodePrWatcherClosureReconcileEffect(HandlerPrWatcherClosureReconcile):
    """ONEX entrypoint: read stale OPEN watcher records and flip the closed ones."""


__all__ = [
    "HandlerPrWatcherClosureObservation",
    "HandlerPrWatcherClosureReconcile",
    "HandlerPrWatcherClosureTick",
    "NodePrWatcherClosureReconcileEffect",
]
