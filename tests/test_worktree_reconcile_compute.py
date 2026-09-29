# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""First-match rules and model rails without I/O."""

import pytest

from omnimarket.events.worktree_reconcile import (
    EnumWorktreeKind as Kind,
)
from omnimarket.events.worktree_reconcile import (
    EnumWorktreeReconcileDecision as Decision,
)
from omnimarket.events.worktree_reconcile import (
    ModelWorktreeDecisionRecord,
    ModelWorktreeFacts,
    ModelWorktreeReconcileDecisions,
    ModelWorktreeReconcilePolicy,
    ModelWorktreeReconcileRequest,
)
from omnimarket.nodes.node_worktree_reconcile_compute.handlers.handler_worktree_reconcile import (
    HandlerWorktreeReconcileCompute,
)
from omnimarket.worktree_reconcile.rules import apply_model_verdicts

pytestmark = pytest.mark.unit


def facts(**updates: object) -> ModelWorktreeFacts:
    return ModelWorktreeFacts(
        host="host",
        path="trees/task/repo",
        root="trees",
        kind=Kind.LINKED_WORKTREE,
        head_sha="abc",
        remote_owner="owner",
        last_activity_age_hours=100,
    ).model_copy(update=updates)


ALLOW = ModelWorktreeReconcilePolicy(allowed_remote_owners=("owner",))


@pytest.mark.parametrize(
    ("updates", "decision", "reason"),
    [
        (
            {"probe_errors": ("unreadable",), "content_merged": True},
            Decision.KEEP,
            "facts_unreadable",
        ),
        ({"live_process": True}, Decision.KEEP, "live_process"),
        ({"live_claim": True}, Decision.KEEP, "live_claim"),
        ({"git_locked": True}, Decision.KEEP, "git_locked"),
        ({"in_progress_operation": "rebase"}, Decision.KEEP, "in_progress_op"),
        ({"last_activity_age_hours": None}, Decision.KEEP, "recent_activity"),
        ({"last_activity_age_hours": 5.9}, Decision.KEEP, "recent_activity"),
        (
            {"dirty_tracked_count": 1, "last_activity_age_hours": 10},
            Decision.KEEP,
            "dirty",
        ),
        (
            {"dirty_tracked_count": 1, "content_merged": True},
            Decision.NEEDS_HUMAN,
            "dirty",
        ),
        ({"untracked_nonjunk_count": 1}, Decision.NEEDS_HUMAN, "dirty"),
        ({"unpushed_stash_count": 1}, Decision.NEEDS_HUMAN, "dirty"),
        ({"kind": Kind.ORPHAN_DIR}, Decision.NEEDS_HUMAN, "orphan_dir"),
        (
            {"kind": Kind.ORPHAN_DIR, "orphan_empty": True},
            Decision.REMOVE,
            "orphan_empty",
        ),
        ({"content_merged": True}, Decision.REMOVE, "content_merged"),
        ({"head_on_remote": True}, Decision.REMOVE, "work_on_remote"),
        ({"commits_not_on_remote": 1}, Decision.PIN_AND_REMOVE, "unpushed_work_stale"),
        (
            {"commits_not_on_remote": 1, "last_activity_age_hours": 71.9},
            Decision.KEEP,
            "not_stale",
        ),
        ({}, Decision.KEEP, "not_stale"),
        ({"kind": Kind.STANDALONE_CLONE}, Decision.NEEDS_HUMAN, "ambiguous"),
        (
            {"kind": Kind.STANDALONE_CLONE, "has_remote": True, "content_merged": True},
            Decision.REMOVE,
            "content_merged",
        ),
        (
            {
                "kind": Kind.STANDALONE_CLONE,
                "has_remote": True,
                "local_branches_all_on_remote": True,
                "head_on_remote": True,
            },
            Decision.REMOVE,
            "work_on_remote",
        ),
    ],
)
def test_rules(updates: dict[str, object], decision: Decision, reason: str) -> None:
    result = HandlerWorktreeReconcileCompute().handle(
        ModelWorktreeReconcileRequest(facts=(facts(**updates),), policy=ALLOW)
    )
    assert result.decisions[0].decision == decision
    assert result.decisions[0].reasons == (reason,)


@pytest.mark.parametrize(
    "updates",
    [
        {"dirty_tracked_count": 1},
        {"untracked_nonjunk_count": 1},
        {"unpushed_stash_count": 1},
        {"kind": Kind.ORPHAN_DIR},
        {"live_process": True},
        {"live_claim": True},
        {"git_locked": True},
        {"in_progress_operation": "merge"},
        {"last_activity_age_hours": None},
        {"last_activity_age_hours": 1},
        {"probe_errors": ("error",)},
    ],
)
def test_model_pin_hard_rails(updates: dict[str, object]) -> None:
    fact = facts(**updates)
    row = ModelWorktreeDecisionRecord(
        path=fact.path, decision=Decision.NEEDS_HUMAN, reasons=("ambiguous",)
    )
    result = apply_model_verdicts(
        ModelWorktreeReconcileDecisions(decisions=(row,)),
        (fact,),
        (row.model_copy(update={"decision": Decision.PIN_AND_REMOVE}),),
    )
    assert result.decisions[0].decision == Decision.NEEDS_HUMAN
    assert result.decisions[0].reasons == ("model_verdict_refused_by_rail",)


@pytest.mark.parametrize("verdict", list(Decision))
def test_model_choices(verdict: Decision) -> None:
    fact = facts()
    row = ModelWorktreeDecisionRecord(
        path=fact.path, decision=Decision.NEEDS_HUMAN, reasons=("ambiguous",)
    )
    result = apply_model_verdicts(
        ModelWorktreeReconcileDecisions(decisions=(row,)),
        (fact,),
        (row.model_copy(update={"decision": verdict}),),
    )
    assert result.decisions[0].decision == (
        Decision.NEEDS_HUMAN if verdict == Decision.REMOVE else verdict
    )


def test_model_cannot_change_nonambiguous_or_exceed_cap() -> None:
    a, b = (
        facts(path="a", head_on_remote=True),
        facts(path="b", kind=Kind.STANDALONE_CLONE),
    )
    request = ModelWorktreeReconcileRequest(
        facts=(a, b), policy=ModelWorktreeReconcilePolicy(max_removals_per_run=1)
    )
    initial = HandlerWorktreeReconcileCompute().handle(request)
    verdicts = tuple(
        ModelWorktreeDecisionRecord(
            path=f.path, decision=Decision.PIN_AND_REMOVE, reasons=()
        )
        for f in (a, b)
    )
    result = apply_model_verdicts(initial, (a, b), verdicts)
    assert result.decisions[0] == initial.decisions[0]
    # A pin preserves HEAD only, so a model can never pin-and-remove a clone.
    assert result.decisions[1].decision == Decision.NEEDS_HUMAN
    assert result.decisions[1].reasons == ("model_verdict_refused_by_rail",)


def test_rule_cap_and_disabled_clone() -> None:
    policy = ModelWorktreeReconcilePolicy(
        max_removals_per_run=0, allow_standalone_clone_removal=False
    )
    result = HandlerWorktreeReconcileCompute().handle(
        ModelWorktreeReconcileRequest(
            facts=(facts(head_on_remote=True), facts(kind=Kind.STANDALONE_CLONE)),
            policy=policy,
        )
    )
    assert [row.reasons for row in result.decisions] == [
        ("removal_cap",),
        ("standalone_removal_disabled",),
    ]


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        (
            {"kind": Kind.STANDALONE_CLONE, "has_remote": True, "head_on_remote": True},
            Decision.KEEP,
        ),
        (
            {
                "kind": Kind.STANDALONE_CLONE,
                "has_remote": True,
                "commits_not_on_remote": 1,
            },
            Decision.NEEDS_HUMAN,
        ),
        (
            {
                "kind": Kind.STANDALONE_CLONE,
                "has_remote": False,
                "commits_not_on_remote": 1,
            },
            Decision.NEEDS_HUMAN,
        ),
    ],
)
def test_clone_rule_order(updates: dict[str, object], expected: Decision) -> None:
    result = HandlerWorktreeReconcileCompute().handle(
        ModelWorktreeReconcileRequest(facts=(facts(**updates),), policy=ALLOW)
    )
    assert result.decisions[0].decision == expected


def test_a_clone_of_a_foreign_owner_is_always_kept() -> None:
    fact = facts(
        kind=Kind.STANDALONE_CLONE,
        has_remote=True,
        remote_owner="someone-else",
        head_on_remote=True,
        local_branches_all_on_remote=True,
        content_merged=True,
    )
    result = HandlerWorktreeReconcileCompute().handle(
        ModelWorktreeReconcileRequest(facts=(fact,), policy=ALLOW)
    )
    assert (result.decisions[0].decision, result.decisions[0].reasons) == (
        Decision.KEEP,
        ("foreign_remote",),
    )
    # With no owner allowed at all, no standalone clone qualifies.
    result = HandlerWorktreeReconcileCompute().handle(
        ModelWorktreeReconcileRequest(
            facts=(fact.model_copy(update={"remote_owner": "owner"}),)
        )
    )
    assert result.decisions[0].reasons == ("foreign_remote",)
