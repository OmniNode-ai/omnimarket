# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The one place that builds node_ci_watch handlers."""

from __future__ import annotations

from typing import TYPE_CHECKING

from omnimarket.nodes.node_ci_watch.handlers.handler_ci_watch import HandlerCiWatch

if TYPE_CHECKING:
    from omnimarket.nodes.node_dispatch_worker.handlers.handler_dispatch_worker import (
        HandlerDispatchWorker,
    )
    from omnimarket.nodes.node_dispatch_worker_execution_effect.handlers.handler_dispatch_worker_execution import (
        HandlerDispatchWorkerExecution,
    )


def build_handler_ci_watch() -> HandlerCiWatch:
    """Build the CI watcher with its constructor defaults."""
    return HandlerCiWatch()


def build_handler_dispatch_worker() -> HandlerDispatchWorker:
    """Build the dispatch worker on demand."""
    from omnimarket.nodes.node_dispatch_worker.handlers.handler_dispatch_worker import (
        HandlerDispatchWorker,
    )

    return HandlerDispatchWorker()


def build_handler_dispatch_worker_execution() -> HandlerDispatchWorkerExecution:
    """Build the dispatch executor on demand."""
    from omnimarket.nodes.node_dispatch_worker_execution_effect.handlers.handler_dispatch_worker_execution import (
        HandlerDispatchWorkerExecution,
    )

    return HandlerDispatchWorkerExecution()
