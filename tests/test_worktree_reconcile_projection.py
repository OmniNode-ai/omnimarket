# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure per-host fold and runtime writer seam."""

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml
from omnibase_infra.runtime.auto_wiring.handler_wiring import _extract_rows_upserted

from omnimarket.events.worktree_reconcile import ModelWorktreeReconcileRunCompletedEvent
from omnimarket.nodes.node_projection_worktree_reconcile.handlers import (
    HandlerProjectionWorktreeReconcile,
    WorktreeReconcileProjectionWriter,
)
from omnimarket.nodes.node_projection_worktree_reconcile.models import (
    ModelWorktreeReconcileProjectionRequest,
)
from omnimarket.projection.runner import BaseProjectionRunner

pytestmark = pytest.mark.unit


def event(**updates: object) -> ModelWorktreeReconcileRunCompletedEvent:
    return ModelWorktreeReconcileRunCompletedEvent(
        host="host",
        correlation_id=UUID(int=1),
        started_at=datetime(2026, 1, 1, tzinfo=UTC),
        finished_at=datetime(2026, 1, 1, 1, tzinfo=UTC),
        scanned=5,
        removed=2,
        pinned_and_removed=1,
        kept=1,
        needs_human=1,
        failures=0,
        freed_bytes=100,
        needs_human_paths=("trees/task/repo",),
    ).model_copy(update=updates)


def test_fold_is_idempotent_and_latest_wins() -> None:
    fold = HandlerProjectionWorktreeReconcile()
    latest = event()
    row = fold.handle(ModelWorktreeReconcileProjectionRequest(event=latest))
    assert row.freed_bytes == 100
    assert row.needs_human_paths == latest.needs_human_paths
    for incoming in (
        latest,
        event(finished_at=latest.finished_at - timedelta(hours=1)),
    ):
        assert (
            fold.handle(
                ModelWorktreeReconcileProjectionRequest(event=incoming, current=row)
            )
            == row
        )
    newer = event(finished_at=latest.finished_at + timedelta(hours=1), freed_bytes=200)
    assert (
        fold.handle(
            ModelWorktreeReconcileProjectionRequest(event=newer, current=row)
        ).freed_bytes
        == 200
    )
    with pytest.raises(ValueError, match="different hosts"):
        fold.handle(
            ModelWorktreeReconcileProjectionRequest(
                event=event(host="another"), current=row
            )
        )


def test_equal_timestamp_tie_breaker() -> None:
    fold = HandlerProjectionWorktreeReconcile()
    higher = event(correlation_id=UUID(int=2))
    row = fold.handle(ModelWorktreeReconcileProjectionRequest(event=higher))
    assert (
        fold.handle(ModelWorktreeReconcileProjectionRequest(event=event(), current=row))
        == row
    )


class RecordingDB:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.loop: asyncio.AbstractEventLoop | None = None
        self.refuse = False

    async def connect(self) -> None:
        self.loop = asyncio.get_running_loop()

    async def close(self) -> None:
        self.loop = None

    async def execute(self, query: str, *params: Any) -> list[dict[str, Any]]:
        assert self.loop == asyncio.get_running_loop()
        self.calls.append((query, params))
        return [] if self.refuse else [{"host": params[0], "finished_at": params[-1]}]


def test_writer_dispatch_and_typed_db_parameters() -> None:
    writer = WorktreeReconcileProjectionWriter()
    db = RecordingDB()
    writer._db = db  # type: ignore[assignment]
    assert isinstance(writer, BaseProjectionRunner)
    assert writer.onex_runtime_inprocess_dispatch
    incoming = event().model_dump(mode="json") | {
        "_topic": writer.topics[0],
        "_offset": 4,
    }
    assert writer.handle(incoming)["rows_upserted"] == 1
    assert db.loop is None
    db.refuse = True
    result = writer.handle(incoming)
    assert result["rows_upserted"] == 0
    assert result["state_write_refused"]
    query, params = db.calls[0]
    assert "ON CONFLICT (host)" in query
    assert isinstance(params[-1], datetime)
    assert isinstance(params[1], UUID)


def test_the_runtime_reads_the_writers_count_as_written() -> None:
    """OMN-19833: the count sits under the key the runtime actually reads."""
    writer = WorktreeReconcileProjectionWriter()
    writer._db = RecordingDB()  # type: ignore[assignment]
    incoming = event().model_dump(mode="json") | {"_topic": writer.topics[0]}
    assert _extract_rows_upserted(writer.handle(incoming)) == 1
    writer._db.refuse = True  # type: ignore[attr-defined]
    assert _extract_rows_upserted(writer.handle(incoming)) == 0


def test_contract_chain_and_registration() -> None:
    root = Path(__file__).resolve().parents[1]
    nodes = root / "src" / "omnimarket" / "nodes"
    effect = yaml.safe_load(
        (nodes / "node_worktree_reconcile_effect" / "contract.yaml").read_text()
    )
    projection = yaml.safe_load(
        (nodes / "node_projection_worktree_reconcile" / "contract.yaml").read_text()
    )
    assert set(projection["event_bus"]["subscribe_topics"]).issubset(
        effect["event_bus"]["publish_topics"]
    )
    assert projection["db_io"]["db_tables"][0]["access"] == "read_write"
    assert [
        entry["handler"]["name"] for entry in projection["handler_routing"]["handlers"]
    ] == ["WorktreeReconcileProjectionWriter"]
    import tomllib

    entrypoints = tomllib.loads((root / "pyproject.toml").read_text())["project"][
        "entry-points"
    ]["onex.nodes"]
    for name in (
        "node_worktree_reconcile_compute",
        "node_projection_worktree_reconcile",
    ):
        assert entrypoints[name] == f"omnimarket.nodes.{name}"
    # The effect runs from a host timer: no subscription, no runtime entry point.
    assert "node_worktree_reconcile_effect" not in entrypoints
    assert effect["event_bus"]["subscribe_topics"] == []
    assert effect["runtime_dispatch"]["external_trigger"] is True
