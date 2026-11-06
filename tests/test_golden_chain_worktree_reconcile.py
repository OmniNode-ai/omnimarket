# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compute to effect events to per-host projection, using injected boundaries."""

import pytest

from omnimarket.events.worktree_reconcile import ModelWorktreeReconcileRequest
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
    writer = WorktreeReconcileProjectionWriter()
    db = RecordingDB()
    writer._db = db  # type: ignore[assignment]
    topic, completed = fake.events[-1]
    written = writer.handle(completed.model_dump(mode="json") | {"_topic": topic})
    assert written["rows_upserted"] == 1
    assert db.calls[0][1][8] == 123


@pytest.mark.unit
def test_golden_chain_completed_event_reports_no_deferred_work() -> None:
    fake = Fakes((facts(head_on_remote=True, size_bytes=123),))
    fake.handler().handle(command())
    _, completed = fake.events[-1]
    assert completed.deferred == 0  # type: ignore[attr-defined]
    assert completed.deferred_paths == ()  # type: ignore[attr-defined]


@pytest.mark.unit
def test_error_chain_a_deferred_tree_reaches_the_event_and_the_projection() -> None:
    """A probe timeout keeps the tree, is counted as deferred and is no failure; the
    completed event still reaches the per-host projection with its needs-human list."""
    deferred = facts(
        path="trees/task/slow", probe_errors=("probe_deferred:probe_timeout",)
    )
    fake = Fakes((deferred,))
    result = fake.handler().handle(command())
    assert not fake.removed
    assert result.decided_events[0].outcome == "kept"
    topic, completed = fake.events[-1]
    assert (completed.failures, completed.deferred) == (0, 1)  # type: ignore[attr-defined]
    assert completed.deferred_paths == ("trees/task/slow",)  # type: ignore[attr-defined]
    writer = WorktreeReconcileProjectionWriter()
    db = RecordingDB()
    writer._db = db  # type: ignore[assignment]
    written = writer.handle(completed.model_dump(mode="json") | {"_topic": topic})
    assert written["rows_upserted"] == 1
