# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Binding factories for node_worktree_reconcile_effect."""

from __future__ import annotations

from omnimarket.nodes.node_worktree_reconcile_effect.handlers.handler_worktree_reconcile import (
    HandlerWorktreeReconcile,
)
from omnimarket.nodes.node_worktree_reconcile_effect.protocols import (
    ProtocolClock,
    ProtocolWorktreeDecider,
    ProtocolWorktreeEventPublisher,
    ProtocolWorktreeFactsProbe,
    ProtocolWorktreePinner,
    ProtocolWorktreeRemover,
)


def create_handler_worktree_reconcile(
    *,
    probe: ProtocolWorktreeFactsProbe,
    decider: ProtocolWorktreeDecider,
    pinner: ProtocolWorktreePinner,
    remover: ProtocolWorktreeRemover,
    clock: ProtocolClock,
    publisher: ProtocolWorktreeEventPublisher,
    decided_topic: str,
    completed_topic: str,
) -> HandlerWorktreeReconcile:
    """Bind the worktree reconciler to its collaborators and topics."""
    return HandlerWorktreeReconcile(
        probe=probe,
        decider=decider,
        pinner=pinner,
        remover=remover,
        clock=clock,
        publisher=publisher,
        decided_topic=decided_topic,
        completed_topic=completed_topic,
    )


__all__ = ["create_handler_worktree_reconcile"]
