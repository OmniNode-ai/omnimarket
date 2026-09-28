# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real Postgres proof of typed bindings, stale-write guard and replay."""

import asyncio
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_projection_worktree_reconcile.handlers import (
    handler_worktree_reconcile_writer as module,
)
from tests.test_worktree_reconcile_projection import event


@pytest.mark.integration
def test_postgres_latest_host_run(
    monkeypatch: pytest.MonkeyPatch, integration_postgres_dsn: str
) -> None:
    # OMN-19399: use the Postgres settings CI provisions, like the PR landing
    # proof, so the normal run executes this test instead of growing the skip set.
    dsn = integration_postgres_dsn
    schema = "reconcile_test_" + uuid4().hex
    migration = (
        Path(module.__file__).parent.parent
        / "migrations"
        / "0000_create_worktree_reconcile_hosts.sql"
    )
    monkeypatch.setattr(
        module, "_UPSERT", module._UPSERT.replace("omninode_internal.", f"{schema}.")
    )

    async def prepare() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(f'CREATE SCHEMA "{schema}"')
            await conn.execute(
                migration.read_text().replace("omninode_internal.", f"{schema}.")
            )
        finally:
            await conn.close()

    async def cleanup() -> None:
        conn = await asyncpg.connect(dsn)
        try:
            await conn.execute(f'DROP SCHEMA "{schema}" CASCADE')
        finally:
            await conn.close()

    asyncio.run(prepare())
    try:
        writer = module.WorktreeReconcileProjectionWriter()
        writer.bind_projection_database_url(dsn)
        latest = event()
        for incoming, expected in (
            (latest, 1),
            (latest, 0),
            (event(finished_at=latest.finished_at - timedelta(hours=1)), 0),
            (event(host="another"), 1),
        ):
            result = writer.handle(
                incoming.model_dump(mode="json") | {"_topic": writer.topics[0]}
            )
            assert result["rows_written"] == expected
    finally:
        asyncio.run(cleanup())
