# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The OMN-17018 done-proofs for the queue progress operator.

Each test here is one of the ticket's falsifiable proofs. Before OMN-17018 the
drainer selected the oldest item, compiled it and left the file untouched, so
``test_three_successive_runs_drain_three_distinct_items`` failed by processing
the same item three times.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.enums.enum_dispatch_queue_phase import EnumDispatchQueuePhase
from omnimarket.enums.enum_dispatch_terminal_reason import (
    EnumDispatchTerminalDisposition,
    EnumDispatchTerminalReason,
)
from omnimarket.nodes.node_dispatch_queue_drainer.handlers import (
    FileDispatchQueueLifecycleLedger,
    HandlerDispatchQueueDrainer,
)
from omnimarket.nodes.node_dispatch_queue_drainer.handlers.dispatch_queue_lifecycle_ledger import (
    LIFECYCLE_DIRNAME,
    blocked_terminal,
)
from omnimarket.nodes.node_dispatch_queue_drainer.models import (
    ModelDispatchQueueDrainerRequest,
    ModelDispatchQueueTerminal,
)
from omnimarket.nodes.node_dispatch_worker import (
    ModelDispatchWorkerCommand,
    ModelDispatchWorkerResult,
)

_T0 = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)


class _MockDispatchWorker:
    """Injected dispatch-worker boundary — the real selection logic still runs."""

    def __init__(self, rejected_reason: str = "") -> None:
        self._rejected_reason = rejected_reason
        self.calls: list[ModelDispatchWorkerCommand] = []

    def handle(
        self, command: ModelDispatchWorkerCommand, **kwargs: Any
    ) -> ModelDispatchWorkerResult:
        self.calls.append(command)
        return ModelDispatchWorkerResult(
            validated_task_description=f"[{command.role}] {command.name}",
            validated_prompt_template=""
            if self._rejected_reason
            else "COMPILED PROMPT",
            proposed_agent_spawn_args={}
            if self._rejected_reason
            else {"name": command.name},
            collision_fence_embeds=[],
            rejected_reason=self._rejected_reason,
        )


def _write_item(queue_dir: Path, name: str, *, repo: str = "omniclaude") -> Path:
    queue_dir.mkdir(parents=True, exist_ok=True)
    path = queue_dir / name
    path.write_text(
        yaml.safe_dump(
            {
                "name": path.stem,
                "team": "offrails",
                "role": "fixer",
                "scope": f"drain {path.stem}",
                "targets": ["OMN-17018"],
                "repo": repo,
            }
        ),
        encoding="utf-8",
    )
    return path


def _omni_home(tmp_path: Path, repo: str = "omniclaude") -> Path:
    omni_home = tmp_path / "omni_home"
    (omni_home / repo).mkdir(parents=True, exist_ok=True)
    return omni_home


@pytest.mark.integration
def test_three_successive_runs_drain_three_distinct_items(tmp_path: Path) -> None:
    """Ticket done-proof 1 — today it processes the same one three times."""
    queue_dir = tmp_path / "queue"
    state_dir = tmp_path / "state"
    omni_home = _omni_home(tmp_path)
    for index, name in enumerate(("a.yaml", "b.yaml", "c.yaml")):
        item = _write_item(queue_dir, name)
        # deterministic ordering independent of filesystem timestamp granularity
        import os

        stamp = 1_700_000_000 + index
        os.utime(item, (stamp, stamp))

    handler = HandlerDispatchQueueDrainer(dispatch_worker=_MockDispatchWorker())
    processed: list[str] = []
    for _ in range(3):
        result = handler.handle(
            ModelDispatchQueueDrainerRequest(
                queue_dir=queue_dir, state_dir=state_dir, omni_home=omni_home
            )
        )
        assert result.status == "compiled"
        processed.append(Path(result.queue_item_path).name)

    assert processed == ["a.yaml", "b.yaml", "c.yaml"]
    assert len(set(processed)) == 3

    # the queue is now drained: nothing is selectable, and no file was deleted
    exhausted = handler.handle(
        ModelDispatchQueueDrainerRequest(
            queue_dir=queue_dir, state_dir=state_dir, omni_home=omni_home
        )
    )
    assert exhausted.status == "empty"
    assert sorted(path.name for path in queue_dir.glob("*.yaml")) == [
        "a.yaml",
        "b.yaml",
        "c.yaml",
    ]


@pytest.mark.integration
def test_compiled_item_is_dispatched_not_counted_as_processed(tmp_path: Path) -> None:
    """DoD 1/2 — a compiled run leaves the item DISPATCHED and awaiting an ack."""
    queue_dir = tmp_path / "queue"
    state_dir = tmp_path / "state"
    item = _write_item(queue_dir, "pending.yaml")
    handler = HandlerDispatchQueueDrainer(dispatch_worker=_MockDispatchWorker())

    result = handler.handle(
        ModelDispatchQueueDrainerRequest(
            queue_dir=queue_dir, state_dir=state_dir, omni_home=_omni_home(tmp_path)
        )
    )

    assert result.status == "compiled"
    assert result.lifecycle_phase is EnumDispatchQueuePhase.DISPATCHED
    record = Path(result.lifecycle_record_path)
    assert record.is_file(), "the transition must be durable, not in-memory"

    ledger = FileDispatchQueueLifecycleLedger(
        state_dir / "dispatch_queue" / LIFECYCLE_DIRNAME
    )
    lifecycle = ledger.load(item)
    assert lifecycle is not None
    assert [transition.phase for transition in lifecycle.transitions] == [
        EnumDispatchQueuePhase.CLAIMED,
        EnumDispatchQueuePhase.DISPATCHED,
    ]
    assert lifecycle.is_pending_acknowledgement(_T0) is True


@pytest.mark.integration
def test_timed_out_dispatch_is_pending_and_distinct_from_an_unstarted_item(
    tmp_path: Path,
) -> None:
    """Ticket done-proof 2 — a forced ack timeout is observably not an unstarted item."""
    queue_dir = tmp_path / "queue"
    state_dir = tmp_path / "state"
    dispatched_item = _write_item(queue_dir, "dispatched.yaml")
    untouched_item = _write_item(queue_dir, "untouched.yaml")

    handler = HandlerDispatchQueueDrainer(dispatch_worker=_MockDispatchWorker())
    result = handler.handle(
        ModelDispatchQueueDrainerRequest(
            queue_item_path=dispatched_item,
            state_dir=state_dir,
            omni_home=_omni_home(tmp_path),
            dispatch_ack_timeout_seconds=1,
        )
    )
    assert result.status == "compiled"

    ledger = FileDispatchQueueLifecycleLedger(
        state_dir / "dispatch_queue" / LIFECYCLE_DIRNAME
    )
    lifecycle = ledger.load(dispatched_item)
    assert lifecycle is not None
    long_after = lifecycle.latest.occurred_at + timedelta(hours=1)

    assert lifecycle.acknowledgement_timed_out(long_after) is True
    assert lifecycle.is_pending_acknowledgement(long_after) is True
    # the unstarted item has no record at all — the two are not confusable
    assert ledger.load(untouched_item) is None

    # and a timed-out dispatch is never re-selected as if untouched
    next_run = handler.handle(
        ModelDispatchQueueDrainerRequest(
            queue_dir=queue_dir, state_dir=state_dir, omni_home=_omni_home(tmp_path)
        )
    )
    assert Path(next_run.queue_item_path) == untouched_item


@pytest.mark.integration
def test_named_item_already_in_flight_is_refused_not_reprocessed(
    tmp_path: Path,
) -> None:
    """An explicitly-named in-flight item reports where it is, and is not redone."""
    queue_dir = tmp_path / "queue"
    state_dir = tmp_path / "state"
    item = _write_item(queue_dir, "held.yaml")
    worker = _MockDispatchWorker()
    handler = HandlerDispatchQueueDrainer(dispatch_worker=worker)
    request = ModelDispatchQueueDrainerRequest(
        queue_item_path=item, state_dir=state_dir, omni_home=_omni_home(tmp_path)
    )

    assert handler.handle(request).status == "compiled"
    second = handler.handle(request)

    assert second.status == "blocked"
    assert "not selectable" in second.blocked_reason
    assert second.lifecycle_phase is EnumDispatchQueuePhase.DISPATCHED
    assert len(worker.calls) == 1, "the worker boundary must not be re-invoked"


@pytest.mark.integration
def test_missing_repo_closes_the_item_as_dependency_failure(tmp_path: Path) -> None:
    """A blocked item reaches TERMINAL with a typed, redispatchable reason."""
    queue_dir = tmp_path / "queue"
    state_dir = tmp_path / "state"
    _write_item(queue_dir, "missing.yaml", repo="omniclaude")
    omni_home = tmp_path / "omni_home"
    omni_home.mkdir()  # repo deliberately absent

    result = HandlerDispatchQueueDrainer(dispatch_worker=_MockDispatchWorker()).handle(
        ModelDispatchQueueDrainerRequest(
            queue_dir=queue_dir, state_dir=state_dir, omni_home=omni_home
        )
    )

    assert result.status == "blocked"
    assert result.lifecycle_phase is EnumDispatchQueuePhase.TERMINAL
    assert result.terminal_disposition is EnumDispatchTerminalDisposition.STOPPED
    assert result.terminal_reason is EnumDispatchTerminalReason.DEPENDENCY_FAILURE
    assert result.auto_redispatchable is True


@pytest.mark.integration
def test_unparseable_item_closes_as_unknown_and_refuses_redispatch(
    tmp_path: Path,
) -> None:
    """An unclassifiable stop escalates: ``unknown`` is never auto-retried."""
    queue_dir = tmp_path / "queue"
    queue_dir.mkdir(parents=True)
    (queue_dir / "broken.yaml").write_text("- not - a - mapping\n", encoding="utf-8")

    result = HandlerDispatchQueueDrainer(dispatch_worker=_MockDispatchWorker()).handle(
        ModelDispatchQueueDrainerRequest(
            queue_dir=queue_dir,
            state_dir=tmp_path / "state",
            omni_home=_omni_home(tmp_path),
        )
    )

    assert result.status == "blocked"
    assert result.terminal_reason is EnumDispatchTerminalReason.UNKNOWN
    assert result.auto_redispatchable is False


@pytest.mark.integration
@pytest.mark.parametrize("reason", list(EnumDispatchTerminalReason))
def test_one_synthetic_lane_per_terminal_reason_records_that_reason(
    tmp_path: Path, reason: EnumDispatchTerminalReason
) -> None:
    """Ticket done-proof 3 — one lane per reason, and the recorded reason matches."""
    queue_dir = tmp_path / "queue"
    state_dir = tmp_path / "state"
    item = _write_item(queue_dir, f"{reason.value}.yaml")
    ledger = FileDispatchQueueLifecycleLedger(
        state_dir / "dispatch_queue" / LIFECYCLE_DIRNAME
    )

    HandlerDispatchQueueDrainer(dispatch_worker=_MockDispatchWorker()).handle(
        ModelDispatchQueueDrainerRequest(
            queue_item_path=item, state_dir=state_dir, omni_home=_omni_home(tmp_path)
        )
    )
    ledger.acknowledge_started(item, actor="lane", now=_T0, detail="lane started")
    closed = ledger.mark_terminal(
        item,
        actor="lane",
        terminal=blocked_terminal(reason),
        now=_T0 + timedelta(minutes=1),
        detail=f"synthetic {reason.value} lane",
    )

    assert closed.phase is EnumDispatchQueuePhase.TERMINAL
    assert closed.terminal is not None
    assert closed.terminal.reason is reason
    assert closed.terminal.auto_redispatchable is reason.auto_redispatchable
    # and the reason survives a round trip through the durable record
    reloaded = ledger.load(item)
    assert reloaded is not None
    assert reloaded.terminal is not None
    assert reloaded.terminal.reason is reason


@pytest.mark.integration
def test_recovery_policy_refuses_cancellation_and_unknown_and_allows_overload(
    tmp_path: Path,
) -> None:
    """Ticket done-proof 3, second half — the policy verdicts, end to end."""
    ledger = FileDispatchQueueLifecycleLedger(tmp_path / "lifecycle")
    verdicts: dict[EnumDispatchTerminalReason, bool] = {}
    for reason in (
        EnumDispatchTerminalReason.DELIBERATE_CANCELLATION,
        EnumDispatchTerminalReason.UNKNOWN,
        EnumDispatchTerminalReason.HOST_OVERLOAD,
    ):
        item = tmp_path / f"{reason.value}.yaml"
        item.write_text("placeholder\n", encoding="utf-8")
        closed = ledger.mark_terminal(
            item,
            actor="sweeper",
            terminal=blocked_terminal(reason),
            now=_T0,
            detail=reason.value,
        )
        assert closed.terminal is not None
        verdicts[reason] = closed.terminal.auto_redispatchable

    assert verdicts == {
        EnumDispatchTerminalReason.DELIBERATE_CANCELLATION: False,
        EnumDispatchTerminalReason.UNKNOWN: False,
        EnumDispatchTerminalReason.HOST_OVERLOAD: True,
    }


@pytest.mark.integration
def test_dry_run_mutates_nothing(tmp_path: Path) -> None:
    """DoD 3 — the flag is real now: it compiles a plan and writes nothing."""
    queue_dir = tmp_path / "queue"
    state_dir = tmp_path / "state"
    item = _write_item(queue_dir, "planned.yaml")
    before = item.read_bytes()
    worker = _MockDispatchWorker()

    result = HandlerDispatchQueueDrainer(dispatch_worker=worker).handle(
        ModelDispatchQueueDrainerRequest(
            queue_dir=queue_dir,
            state_dir=state_dir,
            omni_home=_omni_home(tmp_path),
            dry_run=True,
        )
    )

    assert result.status == "dry_run"
    assert result.dry_run is True
    assert result.dispatch_worker_command is not None, "the plan is still reported"
    assert result.lifecycle_phase is None
    assert result.result_artifact_path == ""
    assert worker.calls == [], "a dry run must not reach the dispatch boundary"
    assert not state_dir.exists(), "a dry run must write nothing under the state dir"
    assert item.read_bytes() == before

    # and the item is still selectable afterwards — nothing was consumed
    live = HandlerDispatchQueueDrainer(dispatch_worker=_MockDispatchWorker()).handle(
        ModelDispatchQueueDrainerRequest(
            queue_dir=queue_dir, state_dir=state_dir, omni_home=_omni_home(tmp_path)
        )
    )
    assert live.status == "compiled"
    assert Path(live.queue_item_path) == item


@pytest.mark.integration
def test_completed_lane_closes_without_a_stop_reason(tmp_path: Path) -> None:
    """The taxonomy describes stops; a completed lane carries no reason at all."""
    ledger = FileDispatchQueueLifecycleLedger(tmp_path / "lifecycle")
    item = tmp_path / "done.yaml"
    item.write_text("placeholder\n", encoding="utf-8")
    closed = ledger.mark_terminal(
        item,
        actor="lane",
        terminal=ModelDispatchQueueTerminal(
            disposition=EnumDispatchTerminalDisposition.COMPLETED
        ),
        now=_T0,
        detail="work finished",
    )
    assert closed.terminal is not None
    assert closed.terminal.reason is None
    assert closed.terminal.auto_redispatchable is False
