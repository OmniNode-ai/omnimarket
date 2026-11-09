# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The eval-run handler the runtime constructs with no arguments (OMN-20577).

``HandlerDelegationEvalRun`` takes its labelled-item source by injection. The runtime builds
a routed handler without arguments, so this subclass binds the factory the runtime needs:
one tenant-scoped reader per command, built from the node overlay ``delegation_eval_sources``.
A source that cannot be built (overlay or variable absent) or read ends the command in a
failed run-completed terminal, never in a silent delivery failure.
"""

from __future__ import annotations

from omnimarket.nodes.node_delegation_eval_run_orchestrator.handlers.handler_delegation_eval_run import (
    HandlerDelegationEvalRun,
)
from omnimarket.nodes.node_delegation_eval_run_orchestrator.sources.source_tenant_labelled_items import (
    labelled_items_for_tenant,
)


class HandlerDelegationEvalRunRuntime(HandlerDelegationEvalRun):
    """``HandlerDelegationEvalRun`` with the runtime's tenant-scoped source bound."""

    def __init__(self, container: object | None = None) -> None:
        super().__init__(items_source_factory=labelled_items_for_tenant)


__all__ = ["HandlerDelegationEvalRunRuntime"]
