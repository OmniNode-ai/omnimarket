# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-14751 -- real-Postgres proof for the intent-classification write path.

WHY THIS EXISTS (OMN-15909 gate, and on its own merits)

``TestAgentSourceSeam`` in ``test_golden_chain_projection_intent_classification``
drives this same handler against an in-memory DB double. That double accepts
whatever Python object it is handed: a ``str`` binds as happily as a
``datetime``, and a column that does not exist is never noticed. The defect
class OMN-15909 was opened for -- an ``.isoformat()`` STRING bound to a
``TIMESTAMPTZ`` param -- is invisible to it and only surfaces as
``asyncpg.exceptions.DataError`` against a live connection.

This B3 diff adds a new column (``agent_source``) to the INSERT/ON CONFLICT of
``IntentClassificationProjectionRunner``, which is exactly that shape of
change, so it gets a real connection rather than a mock.

What is proven here, against a live Postgres with the node's own migration
applied to a disposable schema:

1. ``agent_source`` round-trips end to end -- the column exists in the migration
   as written, and the 7-param INSERT binds positionally onto it. A column/
   placeholder ordering slip (``VALUES ($1..$6, NOW(), $7)`` against
   ``(..., emitted_at, ingested_at, agent_source)``) fails here and nowhere else.
2. ``emitted_at`` arrives as a real ``TIMESTAMPTZ``, not a string -- the exact
   OMN-15909 regression, pinned for this write path.
3. A NULL ``agent_source`` is accepted. Historical rows predate the column, which
   is why it is deliberately excluded from the migration's NOT NULL convergence
   loop; if someone later adds it to that loop, this test goes red.
4. ON CONFLICT updates ``agent_source`` -- a Cursor event correcting an earlier
   sourceless row must not leave the column stale.

Skips (never errors) without a reachable database, mirroring
``test_writer_tenant_isolation_omn14898._connect_or_skip``.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, quote_plus

import asyncpg
import pytest

from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter
from omnimarket.nodes.node_projection_intent_classification.handlers.handler_intent_classification import (
    IntentClassificationProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta

_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_intent_classification"
    / "migrations"
)
# Applied in order, exactly as the forward runner applies them. 0001 is a
# separate artifact rather than an edit to 0000 because 0000's content SHA-256
# is pinned in omnibase_infra's application-migrations.tsv ledger.
_MIGRATION_SQL_FILES = (
    _MIGRATIONS_DIR / "0000_create_intent_classification_events.sql",
    _MIGRATIONS_DIR / "0001_intent_classification_agent_source.sql",
)

_SCHEMA = "omn14751_intent_agent_source_test"

# The recorded C3 live wire event -- the same capture TestAgentSourceSeam is
# seeded from. Note `intent_category`, not `intent_class`: that is the field
# name the live publisher actually emits, and the mismatch this PR fixes.
_C3_CORRELATION_ID = "865bb00d-5f40-406c-b9fa-198a0b5d1c6a"
_C3_EVENT: dict[str, object] = {
    "correlation_id": _C3_CORRELATION_ID,
    "session_id": "c3-proof-865bb00d",
    "intent_category": "code_generation",
    "confidence": 0.91,
    "keywords": ["cursor", "hook", "projection"],
    "emitted_at": "2026-08-11T07:42:13.512000+00:00",
    "agent_source": "cursor",
}


def _pg_env() -> tuple[str, str, str, str, str]:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = os.environ.get("INTEGRATION_POSTGRES_PORT", "5432")
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    return password, host, port, user, db


async def _connect_or_skip() -> asyncpg.Connection:
    password, host, port, user, db = _pg_env()
    if not password:
        pytest.skip(
            "POSTGRES_PASSWORD not set -- skipping intent agent_source DB proof"
        )
    dsn = f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn)
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for intent agent_source DB proof: {exc}")


def _adapter_dsn_for_schema(schema: str) -> str:
    password, host, port, user, db = _pg_env()
    options = quote(f"-c search_path={schema},public")
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
        f"?options={options}"
    )


async def _runner_on_schema(schema: str) -> tuple[
    IntentClassificationProjectionRunner, AsyncpgAdapter
]:
    """The REAL runner, wired to a REAL asyncpg pool scoped to ``schema``."""
    runner = IntentClassificationProjectionRunner()
    adapter = AsyncpgAdapter(dsn=_adapter_dsn_for_schema(schema), min_size=1, max_size=2)
    await adapter.connect()
    runner._db = adapter  # type: ignore[assignment]
    return runner, adapter


@pytest.mark.integration
async def test_real_postgres_intent_write_path_round_trips_agent_source() -> None:
    """Drive the live wire event through the real handler into real Postgres."""
    conn = await _connect_or_skip()
    await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
    adapter: AsyncpgAdapter | None = None
    try:
        await conn.execute(f"SET search_path TO {_SCHEMA}, public")
        for sql_file in _MIGRATION_SQL_FILES:
            await conn.execute(sql_file.read_text(encoding="utf-8"))

        runner, adapter = await _runner_on_schema(_SCHEMA)
        meta = MessageMeta(partition=0, offset=0, fallback_id="")

        # --- 1. Cursor event carrying agent_source, keyed by intent_category ---
        assert await runner.project_event("t", dict(_C3_EVENT), meta) is True

        row = await conn.fetchrow(
            "SELECT correlation_id, session_id, intent_class, confidence, "
            "keywords, emitted_at, agent_source "
            "FROM intent_classification_events WHERE correlation_id = $1",
            _C3_CORRELATION_ID,
        )
        assert row is not None, (
            "the C3 wire event produced no row -- the write path did not land"
        )
        assert row["agent_source"] == "cursor"
        assert row["intent_class"] == "code_generation", (
            "intent_category on the wire must land in the intent_class column"
        )
        assert row["session_id"] == "c3-proof-865bb00d"
        assert list(row["keywords"]) == ["cursor", "hook", "projection"]

        # OMN-15909 regression: a real TIMESTAMPTZ, not a string that merely
        # looks like one. asyncpg returns tz-aware datetime for TIMESTAMPTZ.
        assert isinstance(row["emitted_at"], datetime), (
            f"emitted_at came back as {type(row['emitted_at'])!r}, not datetime"
        )
        assert row["emitted_at"].tzinfo is not None
        assert row["emitted_at"].year == 2026

        # --- 2. Sourceless event: NULL agent_source must be accepted ---
        legacy = dict(_C3_EVENT)
        legacy["correlation_id"] = "omn14751-legacy-no-source"
        legacy.pop("agent_source")
        assert await runner.project_event("t", legacy, meta) is True

        legacy_source = await conn.fetchval(
            "SELECT agent_source FROM intent_classification_events "
            "WHERE correlation_id = $1",
            "omn14751-legacy-no-source",
        )
        assert legacy_source is None, (
            "agent_source must stay nullable -- historical rows predate the "
            "column and it is deliberately excluded from the migration's "
            "NOT NULL convergence loop"
        )

        # --- 3. ON CONFLICT must refresh agent_source, not leave it stale ---
        corrected = dict(_C3_EVENT)
        corrected["correlation_id"] = "omn14751-legacy-no-source"
        corrected["agent_source"] = "claude"
        assert await runner.project_event("t", corrected, meta) is True

        upserted = await conn.fetchval(
            "SELECT agent_source FROM intent_classification_events "
            "WHERE correlation_id = $1",
            "omn14751-legacy-no-source",
        )
        assert upserted == "claude", (
            "ON CONFLICT DO UPDATE must carry agent_source through"
        )
    finally:
        if adapter is not None:
            await adapter.close()
        await conn.execute("SET search_path TO public")
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()
