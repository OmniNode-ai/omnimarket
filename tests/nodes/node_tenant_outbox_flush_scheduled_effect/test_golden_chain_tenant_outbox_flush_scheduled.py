# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The scheduled outbox flush: a due tick runs one pass, a tick inside the interval runs none, and a failed pass is named."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from omnibase_infra.runtime.models.model_runtime_tick import ModelRuntimeTick

from omnimarket.handlers.node_overlay_reader import NodeOverlayError
from omnimarket.nodes.node_tenant_outbox_flush_effect.handlers.handler_tenant_outbox_flush import (
    HandlerTenantOutboxFlush,
)
from omnimarket.nodes.node_tenant_outbox_flush_effect.models.model_tenant_outbox_flush import (
    EnumTenantOutboxFlushOutcome,
    ModelOutboxRow,
    ModelTenantOutboxFlushCommand,
    ModelTenantOutboxFlushResult,
)
from omnimarket.nodes.node_tenant_outbox_flush_scheduled_effect.handlers.handler_tenant_outbox_flush_scheduled import (
    HandlerTenantOutboxFlushScheduled,
)

pytestmark = pytest.mark.unit
NODE = "node_tenant_outbox_flush_scheduled_effect"
T0 = datetime(2026, 11, 27, 6, 0, tzinfo=UTC)


def tick(at: datetime) -> ModelRuntimeTick:
    return ModelRuntimeTick(
        now=at,
        tick_id=uuid4(),
        sequence_number=1,
        scheduled_at=at,
        correlation_id=uuid4(),
        scheduler_id="fixture",
        tick_interval_ms=1000,
    )


def schedule(root: Path) -> dict[str, str]:
    (root / NODE).mkdir()
    (root / NODE / "overlay.yaml").write_text(
        yaml.safe_dump(
            {
                "job_id": "tenant_outbox_flush",
                "run_interval_seconds": 60,
                "grace_seconds": 30,
            }
        ),
        encoding="utf-8",
    )
    return {"ONEX_SKILL_OVERLAY_ROOTS": str(root)}


class FakeStore:
    def __init__(self, rows: list[ModelOutboxRow]) -> None:
        self.rows = rows
        self.marked: list[str] = []

    async def fetch_unpublished(self, limit: int) -> list[ModelOutboxRow]:
        return [r for r in self.rows if r.row_id not in self.marked][:limit]

    async def mark_published(self, row_id: str, at: datetime) -> None:
        self.marked.append(row_id)

    async def record_failure(self, row_id: str, error: str) -> None:
        return None


class FakePublisher:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.fail = fail

    async def publish(self, topic: str, key: str, envelope: dict[str, object]) -> None:
        if self.fail:
            raise RuntimeError("broker refused")
        self.sent.append((topic, key))


def row(n: int) -> ModelOutboxRow:
    return ModelOutboxRow(
        row_id=str(n), envelope={"payload": {"tenant": {"tenant_id": f"t{n}"}}}
    )


def runner(store: FakeStore, publisher: FakePublisher):  # type: ignore[no-untyped-def]
    async def run() -> ModelTenantOutboxFlushResult:
        return await HandlerTenantOutboxFlush(
            store, publisher, topic="onex.tenant.events"
        ).handle(ModelTenantOutboxFlushCommand(command_id=uuid4(), batch_limit=10))

    return run


async def test_due_tick_publishes_rows_and_marks_them(tmp_path: Path) -> None:
    store, pub = FakeStore([row(20), row(21)]), FakePublisher()
    h = HandlerTenantOutboxFlushScheduled(
        environ=schedule(tmp_path), runner=runner(store, pub), clock=lambda: T0
    )
    event = await h.handle(tick(T0))
    assert event is not None
    assert (event.job_id, event.node_name, event.outcome) == (
        "tenant_outbox_flush",
        NODE,
        "completed",
    )
    assert "published 2" in event.detail
    assert store.marked == ["20", "21"]
    assert [t for t, _ in pub.sent] == ["onex.tenant.events"] * 2


async def test_tick_inside_interval_runs_no_pass(tmp_path: Path) -> None:
    store, pub = FakeStore([row(1)]), FakePublisher()
    h = HandlerTenantOutboxFlushScheduled(
        environ=schedule(tmp_path), runner=runner(store, pub), clock=lambda: T0
    )
    assert await h.handle(tick(T0)) is not None
    store.rows.append(row(2))
    assert await h.handle(tick(T0 + timedelta(seconds=59))) is None
    assert store.marked == ["1"]
    assert await h.handle(tick(T0 + timedelta(seconds=60))) is not None
    assert store.marked == ["1", "2"]


async def test_empty_outbox_is_a_completed_run(tmp_path: Path) -> None:
    h = HandlerTenantOutboxFlushScheduled(
        environ=schedule(tmp_path),
        runner=runner(FakeStore([]), FakePublisher()),
        clock=lambda: T0,
    )
    event = await h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "completed"
    assert EnumTenantOutboxFlushOutcome.NOTHING_TO_FLUSH.value in event.detail


async def test_error_publish_failure_is_a_failed_run(tmp_path: Path) -> None:
    store = FakeStore([row(5)])
    h = HandlerTenantOutboxFlushScheduled(
        environ=schedule(tmp_path),
        runner=runner(store, FakePublisher(fail=True)),
        clock=lambda: T0,
    )
    event = await h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert store.marked == []


async def test_error_unreadable_flush_overlay_is_a_failed_run_that_names_it(
    tmp_path: Path,
) -> None:
    async def run() -> ModelTenantOutboxFlushResult:
        raise NodeOverlayError("no flush overlay")

    h = HandlerTenantOutboxFlushScheduled(
        environ=schedule(tmp_path), runner=run, clock=lambda: T0
    )
    event = await h.handle(tick(T0))
    assert event is not None
    assert event.outcome == "failed"
    assert "overlay could not be read" in event.detail


async def test_error_missing_schedule_overlay_returns_nothing(tmp_path: Path) -> None:
    h = HandlerTenantOutboxFlushScheduled(
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path)},
        runner=runner(FakeStore([row(1)]), FakePublisher()),
        clock=lambda: T0,
    )
    assert await h.handle(tick(T0)) is None
