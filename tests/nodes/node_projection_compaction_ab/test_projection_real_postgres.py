# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 against real Postgres: the shipped migration and the shipped upsert SQL.

Uses the shared fixture, which takes the INTEGRATION_POSTGRES service when set.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest

import tests.test_omn19514_ticket_id_projection_real_postgres as _pg
from omnimarket.nodes.node_projection_compaction_ab.handlers.handler_projection_compaction_ab import (
    upsert_sql,
)
from omnimarket.nodes.node_projection_compaction_ab.models import (
    EnumAbWriter,
    load_live_spec,
)

postgres = _pg.postgres
NODE = Path(__file__).parents[3] / "src/omnimarket/nodes/node_projection_compaction_ab"
T0 = datetime(2026, 11, 2, 10, 0, tzinfo=UTC)


def _params(
    writer: EnumAbWriter, values: dict[str, str | int | bool], seconds: int, eid: str
) -> list[object]:
    spec = load_live_spec()
    return [
        "e1",
        "s1",
        "b1",
        *(values.get(c) for c in spec.writers[writer.value].columns),
        T0 + timedelta(seconds=seconds),
        eid,
    ]


@pytest.mark.integration
async def test_upsert_key_out_of_order_real_postgres(postgres: _pg._Postgres) -> None:
    admin = await asyncpg.connect(**postgres.connect_kwargs())
    schema = f"ab_{uuid4().hex}"
    try:
        await admin.execute(f"CREATE SCHEMA {schema}")
        sql = (NODE / "migrations/0000_create_compaction_ab.sql").read_text()
        await admin.execute(sql.replace("omninode_internal", schema))

        async def run(
            writer: EnumAbWriter, values: dict[str, str | int | bool], s: int, e: str
        ) -> str:
            return str(
                await admin.execute(
                    upsert_sql(writer).replace("omninode_internal.", f"{schema}."),
                    *_params(writer, values, s, e),
                )
            )

        first = await run(EnumAbWriter.SCORES, {"hl1_events": 5}, 10, "b")
        assignment = await run(EnumAbWriter.ASSIGNMENT, {"arm": "on"}, 1, "a")
        stale = await run(EnumAbWriter.SCORES, {"hl1_events": 1}, 5, "a")
        duplicate = await run(EnumAbWriter.SCORES, {"hl1_events": 9}, 10, "b")
        assert first == "INSERT 0 1"
        assert assignment == "INSERT 0 1"
        assert stale == "INSERT 0 0"
        assert duplicate == "INSERT 0 0"
        rows = await admin.fetch(f"SELECT * FROM {schema}.compaction_ab_rows")
        assert len(rows) == 1
        assert (rows[0]["hl1_events"], rows[0]["arm"]) == (5, "on")
        newer = await run(EnumAbWriter.SCORES, {"hl1_events": 6}, 10, "c")
        assert newer == "INSERT 0 1"
        assert (
            await admin.fetchval(f"SELECT hl1_events FROM {schema}.compaction_ab_rows")
            == 6
        )
    finally:
        await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin.close()
