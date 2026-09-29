# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real Postgres proof of typed bindings, stale-write guard and replay.

The harness SKIPS without a reachable database; CI provisions one for
integration-marked tests and fails a missing-service skip.
"""

import asyncio
import os
from datetime import timedelta
from pathlib import Path
from urllib.parse import quote_plus
from uuid import uuid4

import asyncpg
import pytest

from omnimarket.nodes.node_projection_worktree_reconcile.handlers import (
    handler_worktree_reconcile_writer as module,
)
from tests.test_worktree_reconcile_projection import event


def _dsn_or_skip() -> str:
    """The Postgres CI provisions for integration-marked tests."""
    secret = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not secret:
        pytest.skip(
            "INTEGRATION_POSTGRES_PASSWORD not set -- skipping the worktree "
            "reconcile write-path proof"
        )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return f"postgresql://{quote_plus(user)}:{quote_plus(secret)}@{host}:{port}/{db}"


@pytest.mark.integration
def test_postgres_latest_host_run(monkeypatch: pytest.MonkeyPatch) -> None:
    dsn = _dsn_or_skip()
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
