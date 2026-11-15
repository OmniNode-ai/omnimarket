# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compute to effect events to per-host projection, using injected boundaries."""

import pytest

from omnimarket.events.worktree_reconcile import ModelWorktreeReconcileRequest
from omnimarket.models.model_worktree_reconcile_policy import (
    ModelWorktreeReconcilePolicy,
)
from omnimarket.nodes.node_projection_worktree_reconcile.handlers import (
    WorktreeReconcileProjectionWriter,
)
from omnimarket.nodes.node_worktree_reconcile_compute.handlers import (
    HandlerWorktreeReconcileCompute,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.handler_worktree_reconcile import (
    HandlerWorktreeReconcile,
)
from tests.test_worktree_reconcile_compute import facts
from tests.test_worktree_reconcile_effect import Fakes, command
from tests.test_worktree_reconcile_projection import RecordingDB


@pytest.mark.unit
def test_golden_chain_worktree_reconcile() -> None:
    fact = facts(head_on_remote=True, size_bytes=123)
    decisions = HandlerWorktreeReconcileCompute().handle(
        ModelWorktreeReconcileRequest(facts=(fact,))
    )
    fake = Fakes((fact,))
    effect: HandlerWorktreeReconcile = fake.handler()
    result = effect.handle(command())
    assert result.decided_events[0].decision == decisions.decisions[0]
    assert result.completed_event.removed == 1
    assert (result.completed_event.deferred, result.completed_event.deferred_paths) == (
        0,
        (),
    )
    writer = WorktreeReconcileProjectionWriter()
    db = RecordingDB()
    writer._db = db  # type: ignore[assignment]
    topic, completed = fake.events[-1]
    written = writer.handle(completed.model_dump(mode="json") | {"_topic": topic})
    assert written["rows_upserted"] == 1
    assert db.calls[0][1][8] == 123


@pytest.mark.unit
def test_error_chain_a_deferred_tree_reaches_the_event_and_the_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A probe timeout keeps the tree and is no failure; its decided event carries the
    deferred error and the completed event still reaches the per-host projection."""
    deferred = facts(
        path="trees/task/slow", probe_errors=("probe_deferred:probe_timeout",)
    )
    fake = Fakes((deferred,))
    result = fake.handler().handle(command())
    assert not fake.removed
    decided = result.decided_events[0]
    assert decided.outcome == "kept"
    assert decided.facts.probe_errors == ("probe_deferred:probe_timeout",)
    assert result.completed_event.failures == 0
    assert result.completed_event.deferred == 1
    assert result.completed_event.deferred_paths == ("trees/task/slow",)
    topic, completed = fake.events[-1]
    writer = WorktreeReconcileProjectionWriter()
    db = RecordingDB()
    monkeypatch.setattr(writer, "_db", db)
    written = writer.handle(completed.model_dump(mode="json") | {"_topic": topic})
    assert written["rows_upserted"] == 1


@pytest.mark.unit
def test_golden_chain_a_dead_and_a_stale_locked_tree_leave_and_are_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Compute decides, the effect removes and names each removal, the projection folds it."""
    dead = facts(
        path="trees/task/dead",
        gitdir_missing=True,
        last_activity_age_hours=100.0,
        size_bytes=40,
    )
    stale = facts(
        path="trees/task/stale",
        worktree_locked=True,
        git_locked=True,
        ledger_known=True,
        content_merged=True,
        last_activity_age_hours=30.0,
        size_bytes=60,
    )
    live = facts(
        path="trees/task/live",
        worktree_locked=True,
        git_locked=True,
        ledger_known=True,
        live_claim=True,
        content_merged=True,
    )
    policy = ModelWorktreeReconcilePolicy(prune_dead_worktrees=True)
    decisions = HandlerWorktreeReconcileCompute().handle(
        ModelWorktreeReconcileRequest(facts=(dead, stale, live), policy=policy)
    )
    fake = Fakes((dead, stale, live))
    result = fake.handler().handle(command(policy=policy))
    assert [row.decision for row in result.decided_events] == list(decisions.decisions)
    assert [row.outcome for row in result.decided_events] == [
        "pruned",
        "removed",
        "kept",
    ]
    done = result.completed_event
    assert done.removed_paths == ("trees/task/dead", "trees/task/stale")
    assert done.pruned_paths == ("trees/task/dead",)
    assert done.unlocked_paths == ("trees/task/stale",)
    assert done.live_locked_paths == ("trees/task/live",)
    assert (done.removed, done.pruned, done.failures, done.freed_bytes) == (
        1,
        1,
        0,
        100,
    )
    topic, completed = fake.events[-1]
    writer = WorktreeReconcileProjectionWriter()
    db = RecordingDB()
    monkeypatch.setattr(writer, "_db", db)
    written = writer.handle(completed.model_dump(mode="json") | {"_topic": topic})
    assert written["rows_upserted"] == 1


@pytest.mark.unit
def test_error_chain_a_removal_that_raises_is_a_failure_and_not_a_named_removal() -> (
    None
):
    dead = facts(
        path="trees/task/dead", gitdir_missing=True, last_activity_age_hours=100.0
    )
    fake = Fakes((dead,))

    def refuse(fact: object, now: object) -> None:
        raise OSError("trash on another filesystem")

    fake.remove = refuse  # type: ignore[method-assign]
    result = fake.handler().handle(
        command(policy=ModelWorktreeReconcilePolicy(prune_dead_worktrees=True))
    )
    assert result.decided_events[0].outcome == "failed"
    assert result.decided_events[0].error == "OSError"
    done = result.completed_event
    assert (done.failures, done.pruned) == (1, 0)
    assert (done.removed_paths, done.pruned_paths, done.unlocked_paths) == ((), (), ())
