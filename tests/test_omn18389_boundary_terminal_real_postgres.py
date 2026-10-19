# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-18389: a boundary failure terminal writes no row and reaches no sink on a
real writer, while a tenant-attributed terminal on the same schema writes one.

Runs against the disposable, migrated Postgres lane the verdict/terminal
ordering proof provisions (the INTEGRATION_POSTGRES_* settings), through a
NOBYPASSRLS writer with correlation-scoped admin readbacks, so a zero count
cannot be an RLS-hidden row. The mock-database twin of the skip is
``test_omn18389_boundary_failure_terminal_not_dead_lettered``.
"""

from __future__ import annotations

from collections.abc import Iterator
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
from omnimarket.projection.runner import MessageMeta
from tests.test_omn18389_boundary_failure_terminal_not_dead_lettered import (
    _BOUNDARY_PAYLOAD_TYPE,
    _delivery,
)
from tests.test_omn18565_ordering_independent_verdict_terminal_rls import (
    _Lane,
    _provision,
    _row_count,
)


@pytest.fixture(scope="module")
def postgres_lane() -> Iterator[_Lane]:
    yield from _provision(apply_migration_under_test=True)


@pytest.mark.integration
async def test_boundary_terminal_writes_no_row_and_attributed_terminal_writes_one(
    monkeypatch: pytest.MonkeyPatch, postgres_lane: _Lane
) -> None:
    monkeypatch.setenv("ONEX_TENANT_ID", "")
    monkeypatch.setattr(get_settings(), "onex_tenant_id", "")
    monkeypatch.setattr(get_settings(), "enforce_tenant_isolation", True)
    tenant = uuid4()
    slug = f"declared-{tenant.hex[:12]}"
    lane = postgres_lane
    with lane.admin.cursor() as cur:
        cur.execute(
            "INSERT INTO tenant_registry_mirror "
            "(tenant_slug, tenant_uuid, status) VALUES (%s, %s::uuid, 'active')",
            (slug, str(tenant)),
        )
        cur.execute("SELECT current_schema()")
        schema = cur.fetchone()[0]
    pool = await asyncpg.create_pool(
        lane.writer_dsn.split("?")[0],
        min_size=1,
        max_size=2,
        server_settings={"search_path": f"{schema},public"},
    )
    published: list[str] = []

    async def capture(topic: str, value: bytes) -> None:
        published.append(topic)

    adapter = AsyncpgAdapter(dsn=lane.writer_dsn)
    adapter._pool = pool
    runner = DelegationProjectionRunner(publish_fn=capture)
    monkeypatch.setattr(runner, "_db", adapter)

    boundary, attributed = str(uuid4()), str(uuid4())
    try:
        ok = await runner.project_event(
            runner._topic_delegate_skill_failed,
            _delivery(boundary, _BOUNDARY_PAYLOAD_TYPE),
            MessageMeta(partition=0, offset=1, fallback_id=boundary),
        )
        assert ok is True
        assert _row_count(lane.admin, boundary) == 0
        assert [t for t in published if "malformed" in t] == [], (
            "the boundary terminal reached a malformed sink"
        )

        terminal = ModelDelegateSkillTerminalProjection(
            status="completed",
            correlation_id=UUID(attributed),
            task_type="summarization",
            tenant_id=slug,
            response="proof",
        )
        await runner.project_event(
            runner._topic_delegate_skill_completed,
            terminal.model_dump(mode="json"),
            MessageMeta(partition=0, offset=2, fallback_id=attributed),
        )
        assert _row_count(lane.admin, attributed) == 1
    finally:
        await pool.close()
