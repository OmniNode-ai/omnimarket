# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""How a run reports the probe errors of one tree (OMN-22799).

Every probe error keeps the tree: facts that could not be read never remove anything. What
differs is what the run says about it. A ``probe_deferred:`` error (a git command that outlived
its budget behind other work) is read again next tick, so it is no failure. A
``probe_needs_human:`` error names a state no tick resolves (a nested repository, no merged pull
request evidence source on this host), so the tree goes to a person. Any other error, alone or
beside these, is a failure.
"""

from typing import Literal

from omnimarket.events.worktree_reconcile import (
    EnumWorktreeReconcileDecision as Decision,
)
from omnimarket.events.worktree_reconcile import (
    ModelWorktreeDecisionRecord,
    ModelWorktreeFacts,
    ModelWorktreeReconcileDecisions,
)

PROBE_DEFERRED_PREFIX = "probe_deferred:"
PROBE_NEEDS_HUMAN_PREFIX = "probe_needs_human:"
ProbeClass = Literal["failed", "needs_human", "deferred"]


def probe_class(facts: ModelWorktreeFacts) -> ProbeClass | None:
    """The class of a tree's probe errors; None when it has none."""
    if not facts.probe_errors:
        return None
    if any(
        not error.startswith((PROBE_DEFERRED_PREFIX, PROBE_NEEDS_HUMAN_PREFIX))
        for error in facts.probe_errors
    ):
        return "failed"
    if any(error.startswith(PROBE_NEEDS_HUMAN_PREFIX) for error in facts.probe_errors):
        return "needs_human"
    return "deferred"


def _held(facts: ModelWorktreeFacts) -> bool:
    return bool(
        facts.live_process
        or facts.live_claim
        or facts.live_unit
        or facts.git_locked
        or facts.in_progress_operation
    )


def route_needs_human(
    decisions: ModelWorktreeReconcileDecisions,
    facts: tuple[ModelWorktreeFacts, ...],
) -> ModelWorktreeReconcileDecisions:
    """Send a tree with a needs-human probe error to a person; never a removal.

    Only a keep that the rules took for unreadable facts changes, and a tree something holds
    (a process, a claim, a unit, a git lock, an operation in progress) stays a keep.
    """
    by_path = {fact.path: fact for fact in facts}
    rows: list[ModelWorktreeDecisionRecord] = []
    for row in decisions.decisions:
        fact = by_path[row.path]
        if (
            row.decision == Decision.KEEP
            and row.reasons == ("facts_unreadable",)
            and probe_class(fact) == "needs_human"
            and not _held(fact)
        ):
            reason = next(
                error.removeprefix(PROBE_NEEDS_HUMAN_PREFIX)
                for error in fact.probe_errors
                if error.startswith(PROBE_NEEDS_HUMAN_PREFIX)
            )
            row = row.model_copy(
                update={"decision": Decision.NEEDS_HUMAN, "reasons": (reason,)}
            )
        rows.append(row)
    return decisions.model_copy(update={"decisions": tuple(rows)})
