# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A row appended in file mode is queued, and replayed once the bus accepts it again."""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.events.model_ledger_row_event import work_ledger_row_id
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.handlers.handler_file_mode_replay import (
    HandlerFileModeReplay,
)
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.handlers.replay_queue import (
    InMemoryReplayQueue,
    JsonlReplayQueue,
    build_replay_queue,
    load_replay_overlay,
)
from omnimarket.nodes.node_work_ledger_file_mode_replay_effect.models.model_file_mode_replay import (
    EnumReplayOperation,
    ModelFileModeReplayRequest,
    ModelFileModeReplayResult,
    ModelReplayPublishOutcome,
)
from tests.node_overlay_support import install_node_overlay

pytestmark = pytest.mark.unit

NODE = "node_work_ledger_file_mode_replay_effect"
ROW = "2026-10-31T10:00:00Z | MSG | lane=alpha | to=orchestrator | file mode row"
ROW_B = "2026-10-31T10:01:00Z | HOLD | lane=beta | surface=x | second file mode row"


class _Bus:
    """A bus that refuses until ``up`` and then accepts; records every publish."""

    def __init__(self) -> None:
        self.up = False
        self.accepted: list[str] = []

    def publish_row(self, row: str, ledger_id: str) -> ModelReplayPublishOutcome:
        if not self.up:
            return ModelReplayPublishOutcome(
                accepted=False, published=False, error="broker unreachable"
            )
        self.accepted.append(row)
        return ModelReplayPublishOutcome(accepted=True, published=True)


def _enqueue(handler: HandlerFileModeReplay, *rows: str) -> None:
    handler.handle(
        ModelFileModeReplayRequest(
            operation=EnumReplayOperation.ENQUEUE, rows=list(rows)
        )
    )


def _drain(handler: HandlerFileModeReplay) -> ModelFileModeReplayResult:
    return handler.handle(
        ModelFileModeReplayRequest(operation=EnumReplayOperation.DRAIN)
    )


def test_file_mode_replay_queue_holds_a_row_while_the_bus_refuses() -> None:
    bus = _Bus()
    queue = InMemoryReplayQueue()
    handler = HandlerFileModeReplay(queue=queue, publisher=bus)
    _enqueue(handler, ROW)
    result = _drain(handler)
    assert result.replayed == []
    assert [p.row_id for p in result.still_pending] == [work_ledger_row_id(ROW)]
    assert result.still_pending[0].attempts == 1
    assert result.still_pending[0].last_reason == "broker unreachable"
    assert queue.pending_row_ids() == {work_ledger_row_id(ROW)}


def test_file_mode_replay_queue_replays_once_the_bus_accepts_again() -> None:
    bus = _Bus()
    queue = InMemoryReplayQueue()
    handler = HandlerFileModeReplay(queue=queue, publisher=bus)
    _enqueue(handler, ROW, ROW_B)
    assert _drain(handler).replayed == []
    bus.up = True
    result = _drain(handler)
    assert result.replayed == [work_ledger_row_id(ROW), work_ledger_row_id(ROW_B)]
    assert result.still_pending == []
    assert bus.accepted == [ROW, ROW_B]  # oldest first, the ledger's own order
    assert queue.pending_row_ids() == set()


def test_file_mode_replay_queue_is_idempotent_by_row_id() -> None:
    bus = _Bus()
    bus.up = True
    handler = HandlerFileModeReplay(queue=InMemoryReplayQueue(), publisher=bus)
    _enqueue(handler, ROW)
    again = handler.handle(
        ModelFileModeReplayRequest(operation=EnumReplayOperation.ENQUEUE, rows=[ROW])
    )
    assert again.enqueued == 0
    assert again.already_queued == 1
    assert _drain(handler).replayed == [work_ledger_row_id(ROW)]
    # A second drain publishes nothing, and re-enqueueing a replayed row does not requeue it.
    _enqueue(handler, ROW)
    assert _drain(handler).replayed == []
    assert bus.accepted == [ROW]


def test_file_mode_replay_queue_accepted_on_the_spool_only_stays_pending() -> None:
    class _SpoolOnly:
        def publish_row(self, row: str, ledger_id: str) -> ModelReplayPublishOutcome:
            return ModelReplayPublishOutcome(accepted=True, published=False)

    queue = InMemoryReplayQueue()
    handler = HandlerFileModeReplay(queue=queue, publisher=_SpoolOnly())
    _enqueue(handler, ROW)
    result = _drain(handler)
    assert result.replayed == []
    assert (
        result.still_pending[0].last_reason
        == "accepted on the spool, not yet published"
    )


def test_file_mode_replay_queue_a_row_that_cannot_be_typed_is_refused_with_its_reason() -> (
    None
):
    class _Refuses:
        def publish_row(self, row: str, ledger_id: str) -> ModelReplayPublishOutcome:
            return ModelReplayPublishOutcome(
                accepted=False, published=False, refusal="unknown row type NOTE"
            )

    queue = InMemoryReplayQueue()
    handler = HandlerFileModeReplay(queue=queue, publisher=_Refuses())
    _enqueue(handler, ROW)
    result = _drain(handler)
    assert result.still_pending == []
    assert [(r.row_id, r.reason) for r in result.refused] == [
        (work_ledger_row_id(ROW), "unknown row type NOTE")
    ]
    # Refused is counted with its reason, and is no longer pending.
    assert queue.pending_row_ids() == set()
    assert queue.refused_reasons() == {work_ledger_row_id(ROW): "unknown row type NOTE"}


def test_file_mode_replay_queue_a_publisher_exception_leaves_the_row_pending() -> None:
    class _Raises:
        def publish_row(self, row: str, ledger_id: str) -> ModelReplayPublishOutcome:
            raise ConnectionError("down")

    handler = HandlerFileModeReplay(queue=InMemoryReplayQueue(), publisher=_Raises())
    _enqueue(handler, ROW)
    result = _drain(handler)
    assert result.still_pending[0].last_reason == "ConnectionError"


def test_file_mode_replay_queue_survives_a_restart_on_disk(tmp_path: Path) -> None:
    path = tmp_path / "queue.jsonl"
    bus = _Bus()
    _enqueue(HandlerFileModeReplay(queue=JsonlReplayQueue(path), publisher=bus), ROW)
    # A new process reads the same queue file and replays what the first one queued.
    bus.up = True
    restarted = HandlerFileModeReplay(queue=JsonlReplayQueue(path), publisher=bus)
    assert _drain(restarted).replayed == [work_ledger_row_id(ROW)]
    assert JsonlReplayQueue(path).pending_row_ids() == set()
    assert bus.accepted == [ROW]


def test_file_mode_replay_queue_overlay_selects_the_backend(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_node_overlay(monkeypatch, tmp_path / "o1", NODE, {"backend": "inmemory"})
    assert isinstance(build_replay_queue(load_replay_overlay()), InMemoryReplayQueue)
    install_node_overlay(
        monkeypatch,
        tmp_path / "o2",
        NODE,
        {"backend": "file", "queue_path": str(tmp_path / "q.jsonl")},
    )
    assert isinstance(build_replay_queue(load_replay_overlay()), JsonlReplayQueue)
    install_node_overlay(monkeypatch, tmp_path / "o3", NODE, {"backend": "file"})
    with pytest.raises(ValueError, match="queue_path"):
        load_replay_overlay()
