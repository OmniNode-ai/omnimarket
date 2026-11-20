# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# unimported-handler-allow: HandlerWorktreePruneEligibility is resolved at boot from handler_routing in contract.yaml, by module path, not by a Python import
"""Which trees a prune run may remove: the shared worktree reconcile classifier under its merged-only rule.

Nothing is classified here. The request goes to ``omnimarket.worktree_reconcile.rules.reconcile`` with the policy's
``merged_only`` forced on, so a tree is removable only when it is clean, holds no work off the remote, is merged
(content merged or its pull request merged at its head) and no live process, claim or unit names it.
"""

from __future__ import annotations

from omnimarket.events.worktree_reconcile import (
    EnumWorktreeReconcileDecision,
    ModelWorktreeReconcileDecisions,
    ModelWorktreeReconcileRequest,
)
from omnimarket.worktree_reconcile.rules import reconcile

REMOVING = frozenset(
    {EnumWorktreeReconcileDecision.REMOVE, EnumWorktreeReconcileDecision.PIN_AND_REMOVE}
)


class HandlerWorktreePruneEligibility:
    """Classify one host's trees for a prune run, merged work only."""

    def handle(
        self, request: ModelWorktreeReconcileRequest
    ) -> ModelWorktreeReconcileDecisions:
        merged_only = request.policy.model_copy(update={"merged_only": True})
        return reconcile(request.model_copy(update={"policy": merged_only}))


def removable_paths(decisions: ModelWorktreeReconcileDecisions) -> tuple[str, ...]:
    return tuple(row.path for row in decisions.decisions if row.decision in REMOVING)


__all__ = ["HandlerWorktreePruneEligibility", "removable_paths"]
