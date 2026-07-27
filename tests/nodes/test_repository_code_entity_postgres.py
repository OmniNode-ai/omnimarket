# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Behaviour tests for RepositoryCodeEntityPostgres (OMN-15230).

Two layers, deliberately:

* Hermetic tests drive a recording pool double. They pin the parts that do not
  need a server — the DSN fail-fast, the empty-batch short circuit, the limit
  guard, and the fact that every statement is parameterised.
* Integration tests (``@pytest.mark.integration``, auto-skipped without
  ``POSTGRES_PASSWORD``) run the *same* SQL against real Postgres through the
  repo's ``postgres_fixture``. They own the claims a double cannot make: that
  the statements parse, that the ``last_embedded_at < last_extracted_at``
  staleness predicate selects what it is supposed to, and that the enrichment
  UPDATE writes the columns it claims.

The integration layer creates its own **session-scoped TEMP** scratch table
rather than assuming a provisioned ``code_entities``: probed 2026-07-27, that
table exists in no database on either the .201 dev or stability lane, so a test
that assumed it would skip forever and prove nothing. TEMP also means the test
leaves no durable footprint in whatever database it is pointed at. The DDL
mirrors the AST-extraction store's shape for the columns this repository touches.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from omnimarket.repositories.repository_code_entity_postgres import (
    CODE_ENTITIES_TABLE,
    CODE_ENTITY_DB_URL_ENV,
    RepositoryCodeEntityPostgres,
)

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------
# hermetic layer
# --------------------------------------------------------------------------


class _RecordingPool:
    """Records every statement/arg pair the repository issues."""

    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or []
        self.fetched: list[tuple[str, tuple[Any, ...]]] = []
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        self.fetched.append((sql, args))
        return self.rows

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return "UPDATE 1"

    async def close(self) -> None:  # pragma: no cover - not exercised
        return None


def _repository(pool: _RecordingPool) -> RepositoryCodeEntityPostgres:
    return RepositoryCodeEntityPostgres(pool=pool)  # type: ignore[arg-type]


async def test_missing_dsn_env_raises_naming_the_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No DSN -> loud OSError naming the env var, never a silent default."""
    monkeypatch.delenv(CODE_ENTITY_DB_URL_ENV, raising=False)
    repository = RepositoryCodeEntityPostgres()

    with pytest.raises(OSError, match=CODE_ENTITY_DB_URL_ENV):
        await repository.get_entities_needing_embedding(limit=10)


async def test_dsn_is_read_from_env_at_first_use_not_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Constructing the repository with no DSN configured must not raise.

    The plugin constructs the repository during kernel boot; raising there would
    take the whole runtime down over a config gap that only matters at dispatch.
    """
    monkeypatch.delenv(CODE_ENTITY_DB_URL_ENV, raising=False)
    repository = RepositoryCodeEntityPostgres()

    monkeypatch.setenv(CODE_ENTITY_DB_URL_ENV, "postgresql://u:p@h:5432/db")
    assert repository._resolve_dsn() == "postgresql://u:p@h:5432/db"


@pytest.mark.parametrize("limit", [0, -1])
async def test_non_positive_limit_is_rejected(limit: int) -> None:
    """LIMIT 0 would look like a drained queue; reject it instead."""
    repository = _repository(_RecordingPool())

    with pytest.raises(ValueError, match="limit must be > 0"):
        await repository.get_entities_needing_embedding(limit=limit)
    with pytest.raises(ValueError, match="limit must be > 0"):
        await repository.get_entities_needing_enrichment(limit=limit)


async def test_update_embedded_at_skips_the_round_trip_on_empty_input() -> None:
    pool = _RecordingPool()
    await _repository(pool).update_embedded_at([])
    assert pool.executed == []


async def test_queries_are_parameterised_and_bind_the_limit() -> None:
    """No value interpolation: the limit must arrive as a bind parameter."""
    pool = _RecordingPool(rows=[{"id": "x"}])
    repository = _repository(pool)

    await repository.get_entities_needing_embedding(limit=7)
    await repository.get_entities_needing_enrichment(limit=9)

    embedding_sql, embedding_args = pool.fetched[0]
    enrichment_sql, enrichment_args = pool.fetched[1]

    assert embedding_args == (7,)
    assert enrichment_args == (9,)
    assert "LIMIT $1" in embedding_sql
    assert "LIMIT $1" in enrichment_sql
    assert "7" not in embedding_sql
    assert "9" not in enrichment_sql
    assert CODE_ENTITIES_TABLE in embedding_sql
    assert CODE_ENTITIES_TABLE in enrichment_sql


async def test_update_enrichment_binds_every_field_positionally() -> None:
    pool = _RecordingPool()
    entity_id = str(uuid4())

    await _repository(pool).update_enrichment(
        entity_id=entity_id,
        classification="handler",
        llm_description="Handles requests.",
        architectural_pattern="handler",
        classification_confidence=0.91,
        enrichment_version="1.2.3",
    )

    sql, args = pool.executed[0]
    assert args == (
        entity_id,
        "handler",
        "Handles requests.",
        "handler",
        0.91,
        "1.2.3",
    )
    assert "last_enriched_at = NOW()" in sql


async def test_injected_pool_is_not_closed_by_the_repository() -> None:
    """An injected pool belongs to its owner; close() must not touch it."""
    closed = False

    class _Pool(_RecordingPool):
        async def close(self) -> None:
            nonlocal closed
            closed = True

    repository = RepositoryCodeEntityPostgres(pool=_Pool())  # type: ignore[arg-type]
    await repository.close()
    assert closed is False


# --------------------------------------------------------------------------
# integration layer — real Postgres
# --------------------------------------------------------------------------

_SCRATCH_TABLE_DDL = """
CREATE TEMP TABLE IF NOT EXISTS {table} (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_name TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    source_repo TEXT NOT NULL,
    source_path TEXT NOT NULL,
    bases TEXT[],
    methods JSONB,
    fields JSONB,
    decorators TEXT[],
    docstring TEXT,
    signature TEXT,
    classification TEXT,
    llm_description TEXT,
    architectural_pattern TEXT,
    classification_confidence FLOAT,
    enrichment_version TEXT,
    last_extracted_at TIMESTAMPTZ DEFAULT NOW(),
    last_enriched_at TIMESTAMPTZ,
    last_embedded_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE(qualified_name, source_repo)
)
"""


class _ConnectionAsPool:
    """Adapts a single asyncpg connection to the pool surface the repository uses."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    async def fetch(self, sql: str, *args: Any) -> Any:
        return await self._conn.fetch(sql, *args)

    async def execute(self, sql: str, *args: Any) -> Any:
        return await self._conn.execute(sql, *args)

    async def close(self) -> None:  # pragma: no cover - owner closes it
        return None


@pytest.fixture
def scratch_table(monkeypatch: pytest.MonkeyPatch) -> str:
    """A unique per-test table name, patched in as the repository's table."""
    table = f"code_entities_omn15230_{uuid4().hex[:12]}"
    monkeypatch.setattr(
        "omnimarket.repositories.repository_code_entity_postgres.CODE_ENTITIES_TABLE",
        table,
    )
    return table


async def _seed(conn: Any, table: str, **overrides: Any) -> str:
    row = {
        "entity_name": "Alpha",
        "entity_type": "class",
        "qualified_name": f"mod.Alpha.{uuid4().hex[:8]}",
        "source_repo": "omnimarket",
        "source_path": "src/mod.py",
        "docstring": "Does a thing.",
        "signature": "class Alpha(Base)",
    }
    row.update(overrides)
    entity_id: str = await conn.fetchval(
        f"""
        INSERT INTO {table}
            (entity_name, entity_type, qualified_name, source_repo, source_path,
             docstring, signature, classification, last_embedded_at,
             last_extracted_at)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, COALESCE($10, NOW()))
        RETURNING id
        """,
        row["entity_name"],
        row["entity_type"],
        row["qualified_name"],
        row["source_repo"],
        row["source_path"],
        row["docstring"],
        row["signature"],
        row.get("classification"),
        row.get("last_embedded_at"),
        row.get("last_extracted_at"),
    )
    return str(entity_id)


@pytest.mark.integration
async def test_real_postgres_roundtrip(
    postgres_fixture: Any,
    scratch_table: str,
) -> None:
    """Every statement runs against real Postgres and selects/writes correctly.

    No in-body skip: ``postgres_fixture`` owns the skip decision, and the
    OMN-14172 fail-closed silent-skip checker treats a service-absence skip
    inside an integration test as RED.
    """
    conn = postgres_fixture
    await conn.execute(_SCRATCH_TABLE_DDL.format(table=scratch_table))
    try:
        repository = RepositoryCodeEntityPostgres(
            pool=_ConnectionAsPool(conn)  # type: ignore[arg-type]
        )

        never_embedded = await _seed(conn, scratch_table)
        stale = await _seed(
            conn,
            scratch_table,
            last_embedded_at=datetime(2020, 1, 1, tzinfo=UTC),
        )
        fresh = await _seed(conn, scratch_table)
        await conn.execute(
            f"UPDATE {scratch_table} SET last_embedded_at = NOW() + interval '1 day' "
            "WHERE id = $1::uuid",
            fresh,
        )
        enriched = await _seed(conn, scratch_table, classification="model")

        # -- embedding selection: never-embedded + stale, never fresh
        pending = await repository.get_entities_needing_embedding(limit=50)
        pending_ids = {str(row["id"]) for row in pending}
        assert never_embedded in pending_ids
        assert stale in pending_ids
        assert fresh not in pending_ids

        # every column the embedding handler reads is present
        sample = next(r for r in pending if str(r["id"]) == never_embedded)
        for column in (
            "entity_name",
            "entity_type",
            "qualified_name",
            "source_repo",
            "source_path",
            "docstring",
            "signature",
            "classification",
            "llm_description",
        ):
            assert column in sample

        # -- update_embedded_at clears them from the pending set
        await repository.update_embedded_at([never_embedded, stale])
        still_pending = {
            str(row["id"])
            for row in await repository.get_entities_needing_embedding(limit=50)
        }
        assert never_embedded not in still_pending
        assert stale not in still_pending

        # -- enrichment selection: classification IS NULL only
        needs_enrichment = await repository.get_entities_needing_enrichment(limit=50)
        enrichment_ids = {str(row["id"]) for row in needs_enrichment}
        assert never_embedded in enrichment_ids
        assert enriched not in enrichment_ids
        for column in ("bases", "methods", "fields", "decorators"):
            assert column in needs_enrichment[0]

        # -- update_enrichment writes every declared column
        await repository.update_enrichment(
            entity_id=never_embedded,
            classification="handler",
            llm_description="Handles requests.",
            architectural_pattern="handler",
            classification_confidence=0.91,
            enrichment_version="1.2.3",
        )
        written = await conn.fetchrow(
            f"""
            SELECT classification, llm_description, architectural_pattern,
                   classification_confidence, enrichment_version, last_enriched_at
            FROM {scratch_table} WHERE id = $1::uuid
            """,
            never_embedded,
        )
        assert written["classification"] == "handler"
        assert written["llm_description"] == "Handles requests."
        assert written["architectural_pattern"] == "handler"
        assert written["classification_confidence"] == pytest.approx(0.91)
        assert written["enrichment_version"] == "1.2.3"
        assert written["last_enriched_at"] is not None

        assert never_embedded not in {
            str(row["id"])
            for row in await repository.get_entities_needing_enrichment(limit=50)
        }
    finally:
        await conn.execute(f"DROP TABLE IF EXISTS {scratch_table}")
