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
