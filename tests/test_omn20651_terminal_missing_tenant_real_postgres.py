# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Missing terminal attribution refuses before SQL on both real writers.

The migrated, disposable Postgres lane comes from the existing verdict/terminal
ordering proof. It uses a NOBYPASSRLS writer and correlation-scoped admin
readbacks, so a zero count cannot be an RLS-hidden row. Every negative control
is paired with a registry-resolved tenant on the same schema and writer.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.config.settings import get_settings
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    ModelProjectionTaskDelegatedEvent,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.tenant_isolation import TenantRequiredError
from tests.test_omn18565_ordering_independent_verdict_terminal_rls import (
    _fetch_row,
    _handler,
    _Lane,
    _provision,
    _row_count,
)

pytestmark = pytest.mark.integration


async def _capture(topic: str, value: bytes) -> None:
    """Capture-only publisher: the proof concerns the durable writer."""


@pytest.fixture(scope="module")
def postgres_lane() -> Iterator[_Lane]:
    yield from _provision(apply_migration_under_test=True)


@pytest.mark.parametrize("path", ["sync", "async"])
@pytest.mark.parametrize("terminal_kind", ["canonical", "delegate_skill"])
@pytest.mark.parametrize("missing", [None, "", "   "])
@pytest.mark.parametrize("enforce", [False, True])
@pytest.mark.parametrize("writer_tenant", ["", "local-writer", str(uuid4())])
async def test_unattributed_terminal_refuses_and_declared_tenant_writes_one_row(
    monkeypatch: pytest.MonkeyPatch,
    postgres_lane: _Lane,
    path: str,
    terminal_kind: str,
    missing: str | None,
    enforce: bool,
    writer_tenant: str,
) -> None:
    # The submitting runtime carries no tenant; writer configuration is a
    # distinct process's setting and must never become event attribution.
    monkeypatch.setenv("ONEX_TENANT_ID", "")
    monkeypatch.setattr(get_settings(), "onex_tenant_id", writer_tenant)
    monkeypatch.setattr(get_settings(), "enforce_tenant_isolation", enforce)
    tenant_a = uuid4()
    slug_a = f"declared-{tenant_a.hex[:12]}"
    lane = postgres_lane
    with lane.admin.cursor() as cur:
        cur.execute(
            "INSERT INTO tenant_registry_mirror "
            "(tenant_slug, tenant_uuid, status) VALUES (%s, %s::uuid, 'active')",
            (slug_a, str(tenant_a)),
        )
        cur.execute("SELECT current_schema()")
        schema = cur.fetchone()[0]
    pool = await asyncpg.create_pool(
        lane.writer_dsn.split("?")[0],
        min_size=1,
        max_size=2,
        server_settings={"search_path": f"{schema},public"},
    )
    adapter = AsyncpgAdapter(dsn=lane.writer_dsn)
    adapter._pool = pool
    runner = DelegationProjectionRunner(publish_fn=_capture)
    runner._db = adapter

    async def project(correlation: str, tenant: str | None) -> None:
        meta = MessageMeta(partition=0, offset=1, fallback_id=correlation)
        if terminal_kind == "canonical":
            event = ModelProjectionTaskDelegatedEvent(
                correlation_id=correlation,
                tenant_id=tenant,
                task_type="summarization",
                delegated_to="node_delegate_skill_orchestrator",
                model_name="proof-model",
                tokens_output=7,
            )
            if path == "sync":
                assert _handler().project(event, lane.adapter).rows_upserted == 1
            else:
                await runner._project_typed_event_async(event, meta)
        else:
            terminal = ModelDelegateSkillTerminalProjection(
                status="completed",
                correlation_id=UUID(correlation),
                task_type="summarization",
                tenant_id=tenant,
                response="proof",
            )
            if path == "sync":
                result = _handler().project_delegate_skill_terminal(
                    terminal, lane.adapter
                )
                assert result.rows_upserted == 1
            else:
                await runner._project_delegate_skill_terminal(
                    terminal.model_dump(mode="json"), meta
                )

    negative, positive = str(uuid4()), str(uuid4())
    try:
        with pytest.raises(TenantRequiredError, match="no tenant_id") as refusal:
            await project(negative, missing)
        assert _row_count(lane.admin, negative) == 0
        await project(positive, slug_a)
        assert _row_count(lane.admin, positive) == 1
        row: dict[str, Any] | None = _fetch_row(lane.admin, positive)
        assert row is not None
        assert str(row["tenant_id"]) == str(tenant_a)
        with lane.admin.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM delegation_events "
                "WHERE correlation_id = %s AND tenant_id::text = %s",
                (positive, writer_tenant),
            )
            assert cur.fetchone()[0] == 0
        print(
            json.dumps(
                {
                    "path": path,
                    "terminal_kind": terminal_kind,
                    "negative": negative,
                    "negative_rows": 0,
                    "refusal_type": type(refusal.value).__name__,
                    "refusal": str(refusal.value),
                    "positive": positive,
                    "positive_rows": 1,
                    "positive_tenant": str(row["tenant_id"]),
                    "writer_tenant_rows": 0,
                }
            )
        )
    finally:
        await pool.close()
