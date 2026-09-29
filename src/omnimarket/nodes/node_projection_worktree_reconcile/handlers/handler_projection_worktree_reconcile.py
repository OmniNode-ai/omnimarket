# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure latest-run fold. Duplicate and older events leave the host unchanged."""

from omnimarket.nodes.node_projection_worktree_reconcile.models import (
    ModelWorktreeReconcileHostRow,
    ModelWorktreeReconcileProjectionRequest,
)


class HandlerProjectionWorktreeReconcile:
    def handle(
        self, request: ModelWorktreeReconcileProjectionRequest
    ) -> ModelWorktreeReconcileHostRow:
        event, current = request.event, request.current
        if current is not None:
            if current.host != event.host:
                raise ValueError("cannot fold different hosts into one row")
            if (current.finished_at, current.correlation_id.int) >= (
                event.finished_at,
                event.correlation_id.int,
            ):
                return current
        return ModelWorktreeReconcileHostRow.model_validate(
            event.model_dump(exclude={"started_at"})
        )
