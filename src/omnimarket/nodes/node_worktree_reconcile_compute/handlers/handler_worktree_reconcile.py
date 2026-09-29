# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Definition-B deterministic worktree reconciliation."""

from omnimarket.events.worktree_reconcile import (
    ModelWorktreeReconcileDecisions,
    ModelWorktreeReconcileRequest,
)
from omnimarket.worktree_reconcile.rules import reconcile


class HandlerWorktreeReconcileCompute:
    def handle(
        self, request: ModelWorktreeReconcileRequest
    ) -> ModelWorktreeReconcileDecisions:
        return reconcile(request)
