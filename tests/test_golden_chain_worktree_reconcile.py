# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compute to effect events to per-host projection, using injected boundaries."""

from datetime import UTC, datetime
from pathlib import Path

import pytest

from omnimarket.events.worktree_reconcile import (
    ModelWorktreeReconcileRequest,
    ModelWorktreeReconcileRunResult,
)
from omnimarket.models.model_worktree_ledger_projection import (
    ModelLedgerProjectionSettings,
)
from omnimarket.models.model_worktree_reconcile_policy import (
    ModelWorktreeReconcilePolicy,
)
from omnimarket.nodes.node_projection_worktree_reconcile.handlers import (
    WorktreeReconcileProjectionWriter,
)
from omnimarket.nodes.node_worktree_reconcile_compute.handlers import (
    HandlerWorktreeReconcileCompute,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_ledger_projection import (
    ProjectionLedgerReader,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_publisher import (
    publish_topics,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.adapter_remover import (
    GitWorktreeRemover,
)
from omnimarket.nodes.node_worktree_reconcile_effect.handlers.handler_worktree_reconcile import (
    HandlerWorktreeReconcile,
)
from tests.test_worktree_reconcile_compute import facts
from tests.test_worktree_reconcile_effect import (
    NO_UNITS,
    FakeMergedPrReader,
    Fakes,
    GitWorktreeFactsProbe,
    command,
    probe_for,
    squash_merged_then_moved_on,
)
from tests.test_worktree_reconcile_ledger_projection import (
    Pages,
    parity_days,
    terminal_answer,
)
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
def test_golden_chain_a_stale_locked_tree_leaves_and_a_dead_tree_stays_and_both_are_named(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stale-locked tree is removed and named; the dead tree stays NEEDS_HUMAN."""
    dead = facts(
        path="trees/task/dead",
        gitdir_missing=True,
        last_activity_age_hours=100.0,
        size_bytes=40,
    )
    stale = facts(
        path="trees/task/stale",
        worktree_locked=True,
        worktree_lock_reason="worktree_lock_reason",
        git_locked=True,
        ledger_known=True,
        owner_finished=True,
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
    policy = ModelWorktreeReconcilePolicy(lane_lock_reasons=("worktree_lock_reason",))
    decisions = HandlerWorktreeReconcileCompute().handle(
        ModelWorktreeReconcileRequest(facts=(dead, stale, live), policy=policy)
    )
    fake = Fakes((dead, stale, live))
    result = fake.handler().handle(command(policy=policy))
    assert [row.decision for row in result.decided_events] == list(decisions.decisions)
    assert [row.outcome for row in result.decided_events] == [
        "needs_human",
        "removed",
        "kept",
    ]
    done = result.completed_event
    assert done.removed_paths == ("trees/task/stale",)
    assert done.unlocked_paths == ("trees/task/stale",)
    assert done.needs_human_paths == ("trees/task/dead",)
    assert done.live_locked_paths == ("trees/task/live",)
    assert (done.removed, done.failures, done.freed_bytes) == (1, 0, 60)
    topic, completed = fake.events[-1]
    writer = WorktreeReconcileProjectionWriter()
    db = RecordingDB()
    monkeypatch.setattr(writer, "_db", db)
    written = writer.handle(completed.model_dump(mode="json") | {"_topic": topic})
    assert written["rows_upserted"] == 1
    assert "pruned" not in written


@pytest.mark.unit
def test_error_chain_a_removal_that_raises_is_a_failure_and_not_a_named_removal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    removable = facts(
        path="trees/task/removable",
        content_merged=True,
        last_activity_age_hours=100.0,
    )
    fake = Fakes((removable,))

    def refuse(fact: object, now: object) -> None:
        raise OSError("trash on another filesystem")

    monkeypatch.setattr(fake, "remove", refuse)
    result = fake.handler().handle(command())
    assert result.decided_events[0].outcome == "failed"
    assert result.decided_events[0].error == "OSError"
    done = result.completed_event
    assert done.failures == 1
    assert (done.removed_paths, done.unlocked_paths) == ((), ())


@pytest.mark.unit
def test_golden_chain_a_read_ledger_names_no_unread_reason() -> None:
    """A fresh ledger read that proves the removal names no unread reason anywhere:
    the tree leaves, the run is clean and the receipt still reaches the projection."""
    fact = facts(head_on_remote=True, size_bytes=123)
    decisions = HandlerWorktreeReconcileCompute().handle(
        ModelWorktreeReconcileRequest(facts=(fact,))
    )
    fake = Fakes((fact,))
    effect: HandlerWorktreeReconcile = fake.handler()
    result = effect.handle(command())
    assert result.decided_events[0].decision == decisions.decisions[0]
    assert result.decided_events[0].outcome == "removed"
    assert result.completed_event.removed == 1
    assert result.completed_event.ledger_unread == ""
    assert result.errors == ()
    writer = WorktreeReconcileProjectionWriter()
    db = RecordingDB()
    writer._db = db  # type: ignore[assignment]
    topic, completed = fake.events[-1]
    written = writer.handle(completed.model_dump(mode="json") | {"_topic": topic})
    assert written["rows_upserted"] == 1
    assert db.calls[0][1][8] == 123


@pytest.mark.unit
def test_error_chain_an_unread_ledger_keeps_the_unlocked_tree_and_names_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unread ledger keeps the tree and fails the run with the reader's reason; a
    host that names no ledger reports "not_declared" without failing, and either
    completed event still reaches the per-host projection."""
    frozen = facts(
        path="trees/task/frozen",
        head_on_remote=True,
        ledger_known=False,
        ledger_unread_reason="frozen",
    )
    fake = Fakes((frozen,))
    result = fake.handler().handle(command())
    decided = result.decided_events[0]
    assert decided.decision.reasons == ("ledger_unread",)
    assert decided.outcome == "kept"
    assert fake.removed == []
    assert result.completed_event.ledger_unread == "frozen"
    assert result.errors == ("ledger_unread:frozen",)
    assert result.completed_event.failures == 1
    topic, completed = fake.events[-1]
    writer = WorktreeReconcileProjectionWriter()
    db = RecordingDB()
    monkeypatch.setattr(writer, "_db", db)
    written = writer.handle(completed.model_dump(mode="json") | {"_topic": topic})
    assert written["rows_upserted"] == 1
    # CONTROL: the same unread tree on a host that names no ledger reports
    # "not_declared", fails nothing, and removes nothing either.
    control = Fakes(
        (
            facts(
                path="trees/task/frozen",
                head_on_remote=True,
                ledger_known=False,
                ledger_unread_reason="not_declared",
            ),
        )
    )
    control_result = control.handler().handle(command())
    assert control_result.decided_events[0].decision.reasons == ("ledger_unread",)
    assert control_result.completed_event.ledger_unread == "not_declared"
    assert control_result.errors == ()
    assert control_result.completed_event.failures == 0
    assert control.removed == []
    topic, completed = control.events[-1]
    writer = WorktreeReconcileProjectionWriter()
    db = RecordingDB()
    monkeypatch.setattr(writer, "_db", db)
    written = writer.handle(completed.model_dump(mode="json") | {"_topic": topic})
    assert written["rows_upserted"] == 1


CLOCK = datetime(2100, 1, 1, tzinfo=UTC)
OTHER_LANE_CLAIM = (
    "2099-12-31T20:00:00Z | CLAIM | lane=other | ticket=OMN-1 | unrelated work"
)


def _run_on_the_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, terminal_complete: bool
) -> tuple[ModelWorktreeReconcileRunResult, Fakes, Path]:
    """A merged, clean tree on a host whose ledger is the work_ledger_rows projection."""
    root, path, head = squash_merged_then_moved_on(tmp_path)
    merged = FakeMergedPrReader(frozenset({head}))
    probe_for(merged, monkeypatch)  # patches the process and clone-refresh probes
    answer = terminal_answer(
        (OTHER_LANE_CLAIM,),
        now=CLOCK,
        days=parity_days(terminal=terminal_complete, now=CLOCK),
    )
    probe = GitWorktreeFactsProbe(
        unit_reader=NO_UNITS,
        merged_pr_reader=merged,
        ledger_reader=ProjectionLedgerReader(call=Pages(answer)),
    )
    fake = Fakes(())
    monkeypatch.setattr(fake, "now", lambda: CLOCK)
    decided, completed = publish_topics()
    handler = HandlerWorktreeReconcile(
        probe=probe,
        decider=fake,
        pinner=fake,
        remover=GitWorktreeRemover(),
        clock=fake,
        publisher=fake,
        decided_topic=decided,
        completed_topic=completed,
    )
    result = handler.handle(
        command(
            roots=(str(root),),
            decider_command=None,
            pin_command=None,
            merged_pr_command=["reader", "{slug}"],
            ledger_path=None,
            ledger_source="projection",
            ledger_projection=ModelLedgerProjectionSettings(
                bus_lane="dev", timeout_seconds=30.0
            ),
        )
    )
    return result, fake, path


@pytest.mark.unit
def test_golden_chain_a_complete_projection_vouches_and_the_merged_tree_leaves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, fake, path = _run_on_the_projection(
        tmp_path, monkeypatch, terminal_complete=True
    )
    assert result.decided_events[0].outcome == "removed"
    assert not path.exists()
    assert result.completed_event.ledger_unread == ""
    assert result.errors == ()
    assert result.completed_event.failures == 0
    assert result.completed_event.removed_paths == (str(path),)
    topic, completed = fake.events[-1]
    writer = WorktreeReconcileProjectionWriter()
    writer._db = RecordingDB()  # type: ignore[assignment]
    written = writer.handle(completed.model_dump(mode="json") | {"_topic": topic})
    assert written["rows_upserted"] == 1


@pytest.mark.unit
def test_error_chain_an_incomplete_projection_keeps_every_tree_and_names_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    result, fake, path = _run_on_the_projection(
        tmp_path, monkeypatch, terminal_complete=False
    )
    reason = f"projection_incomplete:TERMINAL@{parity_days(now=CLOCK)[0].day}"
    assert result.decided_events[0].outcome == "kept"
    assert result.decided_events[0].decision.reasons == ("ledger_unread",)
    assert path.exists()
    assert result.completed_event.ledger_unread == reason
    assert result.errors == (f"ledger_unread:{reason}",)
    assert result.completed_event.failures == 1
    assert result.completed_event.removed_paths == ()
    topic, completed = fake.events[-1]
    writer = WorktreeReconcileProjectionWriter()
    writer._db = RecordingDB()  # type: ignore[assignment]
    written = writer.handle(completed.model_dump(mode="json") | {"_topic": topic})
    assert written["rows_upserted"] == 1
