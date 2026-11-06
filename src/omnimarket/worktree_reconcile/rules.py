# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared pure rules used by the compute entry and the effect orchestration."""

from omnimarket.events.worktree_reconcile import (
    EnumDecidedBy,
    ModelWorktreeDecisionRecord,
    ModelWorktreeFacts,
    ModelWorktreeReconcileDecisions,
    ModelWorktreeReconcileRequest,
)
from omnimarket.events.worktree_reconcile import (
    EnumWorktreeKind as Kind,
)
from omnimarket.events.worktree_reconcile import (
    EnumWorktreeReconcileDecision as Decision,
)
from omnimarket.handlers import rules_worktree_removal as removal
from omnimarket.models.model_worktree_reconcile_policy import (
    ModelWorktreeReconcilePolicy,
)


def live_reason(
    facts: ModelWorktreeFacts, policy: ModelWorktreeReconcilePolicy
) -> str | None:
    for reason in ("live_process", "live_claim", "live_unit"):
        if getattr(facts, reason):
            return reason
    if facts.git_locked:
        return "git_locked"
    if facts.in_progress_operation:
        return "in_progress_op"
    if (
        facts.last_activity_age_hours is None
        or facts.last_activity_age_hours < policy.quiet_hours
    ):
        return "recent_activity"
    return None


def classify(
    facts: ModelWorktreeFacts, policy: ModelWorktreeReconcilePolicy
) -> tuple[Decision, str]:
    if not facts.facts_complete:
        return Decision.KEEP, "facts_unreadable"
    live = live_reason(facts, policy)
    if live:
        return Decision.KEEP, live
    age = facts.last_activity_age_hours
    assert age is not None
    if facts.dirty:
        return (
            Decision.NEEDS_HUMAN if age >= policy.stale_hours else Decision.KEEP
        ), "dirty"
    if facts.kind == Kind.ORPHAN_DIR:
        return (
            (Decision.REMOVE, "orphan_empty")
            if facts.orphan_empty
            else (Decision.NEEDS_HUMAN, "orphan_dir")
        )
    if (
        facts.kind == Kind.STANDALONE_CLONE
        and not policy.allow_standalone_clone_removal
    ):
        return Decision.KEEP, "standalone_removal_disabled"
    if (
        facts.kind == Kind.STANDALONE_CLONE
        and facts.has_remote
        and facts.remote_owner not in policy.allowed_remote_owners
    ):
        return Decision.KEEP, "foreign_remote"
    if facts.content_merged:
        return Decision.REMOVE, "content_merged"
    if facts.kind == Kind.LINKED_WORKTREE and facts.merged_pr_head:
        return Decision.REMOVE, "pr_merged_head"
    if (
        facts.head_on_remote
        and facts.commits_not_on_remote == 0
        and (facts.kind != Kind.STANDALONE_CLONE or facts.local_branches_all_on_remote)
    ):
        return Decision.REMOVE, "work_on_remote"
    if facts.commits_not_on_remote > 0 and age >= policy.stale_hours:
        # A pin preserves HEAD only; a clone may hold other unpushed branches.
        if facts.kind == Kind.STANDALONE_CLONE:
            return Decision.NEEDS_HUMAN, "clone_unpushed_work"
        return Decision.PIN_AND_REMOVE, "unpushed_work_stale"
    if facts.kind == Kind.STANDALONE_CLONE and not facts.has_remote:
        return Decision.NEEDS_HUMAN, "ambiguous"
    return Decision.KEEP, "not_stale"


def reconcile(
    request: ModelWorktreeReconcileRequest,
) -> ModelWorktreeReconcileDecisions:
    rows = []
    for facts in request.facts:
        decision, reason = removal.hold_unmerged(
            *classify(facts, request.policy), request.policy
        )
        rows.append(
            ModelWorktreeDecisionRecord(
                path=facts.path, decision=decision, reasons=(reason,)
            )
        )
    return removal.cap_removals(
        ModelWorktreeReconcileDecisions(decisions=tuple(rows), policy=request.policy)
    )


def apply_model_verdicts(
    decisions: ModelWorktreeReconcileDecisions,
    facts: tuple[ModelWorktreeFacts, ...],
    verdicts: tuple[ModelWorktreeDecisionRecord, ...],
) -> ModelWorktreeReconcileDecisions:
    """Treat model advice as untrusted; only ambiguous rows can be changed."""
    by_path = {fact.path: fact for fact in facts}
    proposed = {row.path: row for row in verdicts}
    rows = []
    for row in decisions.decisions:
        verdict = proposed.get(row.path)
        if row.decision != Decision.NEEDS_HUMAN or verdict is None:
            rows.append(row)
            continue
        fact = by_path[row.path]
        refused = (
            verdict.decision == Decision.REMOVE
            or removal.refuses_model_removal(verdict.decision, decisions.policy)
            or (
                verdict.decision == Decision.PIN_AND_REMOVE
                and (
                    not fact.facts_complete
                    or fact.dirty
                    or fact.kind == Kind.ORPHAN_DIR
                    or live_reason(fact, decisions.policy) is not None
                    # A pin preserves HEAD only, never a clone's other branches.
                    or fact.kind == Kind.STANDALONE_CLONE
                )
            )
        )
        rows.append(
            ModelWorktreeDecisionRecord(
                path=row.path,
                decision=Decision.NEEDS_HUMAN if refused else verdict.decision,
                decided_by=EnumDecidedBy.MODEL,
                reasons=("model_verdict_refused_by_rail",)
                if refused
                else ("model_verdict",),
                model_rationale=verdict.model_rationale,
            )
        )
    return removal.cap_removals(decisions.model_copy(update={"decisions": tuple(rows)}))
