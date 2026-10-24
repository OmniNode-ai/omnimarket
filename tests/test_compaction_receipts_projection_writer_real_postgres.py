# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres receipts proof with the node's own migrations and grants.

The shared fixture uses CI's INTEGRATION_POSTGRES service when configured,
otherwise an owned, disposable PostgreSQL container. Only schema and role names
are isolated; the SQL, asyncpg parameter binding and grants are the shipped ones.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
from pydantic import ValidationError

import tests.test_omn19514_ticket_id_projection_real_postgres as _pg
from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_compaction_receipts.handlers import (
    handler_compaction_receipts_writer as writer_module,
)
from omnimarket.projection.runner import MessageMeta
from tests.test_compaction_receipts_projection_writer import NODE, _recorded

postgres = _pg.postgres


@pytest.mark.integration
async def test_compaction_receipts_projection_writer_real_postgres_verdicts_replay_and_resolution(
    postgres: _pg._Postgres, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = await asyncpg.connect(**postgres.connect_kwargs())
    suffix = uuid4().hex
    schema, role = f"receipts_{suffix}", f"receipts_writer_{suffix}"
    pool: asyncpg.Pool | None = None
    snapshots: list[dict[str, Any]] = []

    async def capture(exposure: Any, **kwargs: Any) -> bool:
        snapshots.append({"topic": exposure.topic, **kwargs})
        return True

    try:
        await admin.execute(f"CREATE SCHEMA {schema}")
        await admin.execute(f"CREATE ROLE {role} NOLOGIN NOSUPERUSER NOBYPASSRLS")
        for migration in sorted((NODE / "migrations").glob("*.sql")):
            await admin.execute(
                migration.read_text()
                .replace("omninode_internal", schema)
                .replace("omninode_runtime", role)
            )
        for name in ("_SELECT_PRIOR", "_UPSERT"):
            monkeypatch.setattr(
                writer_module,
                name,
                getattr(writer_module, name).replace(
                    "omninode_internal.", f"{schema}."
                ),
            )
        pool = await asyncpg.create_pool(
            **postgres.connect_kwargs(),
            min_size=1,
            max_size=2,
            server_settings={"role": role},
        )
        assert pool is not None
        adapter = AsyncpgAdapter()
        monkeypatch.setattr(adapter, "_pool", pool)
        writer = writer_module.CompactionReceiptsProjectionWriter()
        monkeypatch.setattr(writer, "_db", adapter)
        monkeypatch.setattr(writer, "publish_snapshot_delta", capture)
        topic = writer.subscribe_topics[0]
        meta = MessageMeta(0, 7, "recorded", topic)
        raw = _recorded()
        assert await writer.project_event(topic, raw, meta)
        rows = await admin.fetch(
            f"SELECT * FROM {schema}.compaction_receipts ORDER BY receipt_key"
        )
        assert len(rows) == 5
        assert {row["verdict"] for row in rows} == {
            "installed",
            "install_mismatch",
            "not_installed",
            "uncertain",
        }
        (installed,) = [row for row in rows if row["verdict"] == "installed"]
        assert installed["boundary_uuid"] == raw["boundaries"][0]["boundary_uuid"]
        assert installed["activation_id"] == UUID(raw["boundaries"][0]["activation_id"])
        assert installed["installed_messages_hash"] == installed["observed_hash"]
        assert installed["source_topic"] == topic
        assert isinstance(installed["projected_at"], datetime)
        assert installed["projected_at"].tzinfo is not None
        assert isinstance(installed["projection_cursor"], int)
        assert len(snapshots) == 5

        # Replay and a concurrent delivery after the SELECT both meet the SQL
        # guard. Neither can duplicate or revise a decided boundary.
        assert await writer.project_event(topic, raw, meta)
        assert len(snapshots) == 5
        values = (
            *(installed[column] for column in writer_module._COLUMNS),
            topic,
        )
        assert await asyncio.gather(
            adapter.execute(writer_module._UPSERT, *values),
            adapter.execute(writer_module._UPSERT, *values),
        ) == [[], []]

        # A fuller capture resolves the existing unreadable boundary and
        # advances the read cursor under the non-superuser's sequence grant.
        old = next(row for row in rows if row["reason"] == "unparseable_record")
        boundary_uuid = old["boundary_uuid"]
        fuller = {
            **raw,
            "boundaries": [{**raw["boundaries"][0], "boundary_uuid": boundary_uuid}],
            "facts": [{**raw["facts"][0], "boundary_uuid": boundary_uuid}],
            "current": {},
        }
        assert await writer.project_event(
            topic, fuller, MessageMeta(0, 8, "fuller", topic)
        )
        updated = await admin.fetchrow(
            f"SELECT * FROM {schema}.compaction_receipts WHERE boundary_uuid = $1",
            boundary_uuid,
        )
        assert updated is not None
        assert updated["verdict"] == "installed"
        assert updated["receipt_key"] == old["receipt_key"]
        assert updated["projection_cursor"] > old["projection_cursor"]
        assert (
            await admin.fetchval(f"SELECT COUNT(*) FROM {schema}.compaction_receipts")
            == 5
        )
        with pytest.raises(ValidationError):
            await writer.project_event(topic, {**raw, "unexpected": "refuse"}, meta)
        assert (
            await admin.fetchval(f"SELECT COUNT(*) FROM {schema}.compaction_receipts")
            == 5
        )
    finally:
        if pool is not None:
            await pool.close()
        await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin.execute(f"DROP ROLE IF EXISTS {role}")
        await admin.close()
