# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A missing overlay or a missing outbox column is refused by name; nothing here lets the runtime crash."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_tenant_outbox_flush_effect import (
    runtime_tenant_outbox_flush as entry,
)
from omnimarket.nodes.node_tenant_outbox_flush_effect.handlers.handler_tenant_outbox_flush import (
    HandlerTenantOutboxFlush,
)
from omnimarket.nodes.node_tenant_outbox_flush_effect.handlers.tenant_outbox_postgres import (
    TenantOutboxPostgres,
)
from omnimarket.nodes.node_tenant_outbox_flush_effect.models.model_tenant_outbox_flush import (
    EnumTenantOutboxFlushOutcome,
    ModelTenantOutboxFlushCommand,
)

pytestmark = pytest.mark.unit

ALL = ["id", "envelope", "created_at", "published_at", "attempts", "last_error"]


class ColumnsPool:
    """A pool whose information_schema answer is the column list it was given."""

    def __init__(self, columns: list[str]) -> None:
        self._columns = columns
        self.statements: list[str] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        if "information_schema.columns" in sql:
            return [{"column_name": c} for c in self._columns]
        self.statements.append(sql)
        return []

    async def execute(self, sql: str, *args: Any) -> str:
        self.statements.append(sql)
        return "UPDATE 0"


def _handler(columns: list[str]) -> tuple[HandlerTenantOutboxFlush, ColumnsPool]:
    pool = ColumnsPool(columns)
    store = TenantOutboxPostgres(pool=cast(asyncpg.Pool, pool))

    class Bus:
        async def publish(self, topic: str, key: str, envelope: dict[str, Any]) -> None:
            return None

    return HandlerTenantOutboxFlush(store, Bus(), topic="t"), pool


def _flush(handler: HandlerTenantOutboxFlush) -> Any:
    return asyncio.run(
        handler.handle(ModelTenantOutboxFlushCommand(command_id=uuid4(), batch_limit=5))
    )


def test_a_missing_overlay_is_refused_naming_the_node_and_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path))
    assert entry.main(["--run"]) == 3
    err = capsys.readouterr().err
    assert "node_tenant_outbox_flush_effect" in err
    assert "overlay.yaml" in err


@pytest.mark.parametrize("missing", ["published_at", "envelope", "created_at", "id"])
def test_a_missing_outbox_column_is_refused_naming_the_column(missing: str) -> None:
    result = _flush(_handler([c for c in ALL if c != missing])[0])
    assert result.outcome is EnumTenantOutboxFlushOutcome.FAILED_STORE_UNAVAILABLE
    assert missing in result.detail


def test_a_missing_table_is_refused_naming_the_table() -> None:
    result = _flush(_handler([])[0])
    assert result.outcome is EnumTenantOutboxFlushOutcome.FAILED_STORE_UNAVAILABLE
    assert "tenant_event_outbox" in result.detail


def test_a_refused_shape_sends_no_row_statement() -> None:
    handler, pool = _handler([c for c in ALL if c != "published_at"])
    _flush(handler)
    assert pool.statements == []


def test_a_closer_that_raises_is_a_typed_exit_not_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom() -> None:
        raise RuntimeError("close failed")

    handler = HandlerTenantOutboxFlush()
    monkeypatch.setattr(entry, "load_overlay", lambda: SimpleNamespace(batch_limit=5))

    def build(
        _overlay: object, topic: str
    ) -> tuple[HandlerTenantOutboxFlush, list[Any]]:
        assert topic
        return handler, [boom]

    monkeypatch.setattr(entry, "build_flush", build)
    assert entry.main(["--run"]) in (1, 3)
