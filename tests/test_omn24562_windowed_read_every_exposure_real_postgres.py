# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Every exposure that declares a ``freshness_column`` answers a windowed read (OMN-24562).

A windowed read was once verified against one exposure, so a TEXT freshness
column (``metering_summary.as_of``) answered 503 on ``window=7d`` for 30 hours.
This module walks every node contract whose ``projection_api`` declares a
``freshness_column``, builds the exposure from the node's own migrations on real
PostgreSQL, and issues the 1d, 7d and 30d windowed read through the real store
path (``read_projection_page`` over ``TableRowSource``), requiring a non-error
answer. The planted control reads the same exposure through the window
expression as it stood before OMN-21288 and must fail.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import yaml

from omnimarket.projection.discovery import (
    load_projection_exposures_from_contract,
)
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.read_page import read_projection_page
from omnimarket.projection.table_reader import TableRowSource
from tests.test_omn21970_daily_and_cursor_real_postgres import (
    _ROLES_SQL,
    _Postgres,
)
from tests.test_omn21970_daily_and_cursor_real_postgres import postgres as postgres

pytestmark = pytest.mark.integration

_NODES = Path(__file__).resolve().parents[1] / "src/omnimarket/nodes"
_TENANT = "0af28cd0-0654-42e0-a050-358ee8240940"
_WINDOWS = ("1d", "7d", "30d")
# A node whose views read a table another node's migrations own.
_READS_FROM = {"node_projection_savings": ("node_projection_delegation",)}
_EXTRA_ROLES_SQL = """
DO $$
DECLARE r text;
BEGIN
  FOREACH r IN ARRAY ARRAY['omninode_runtime', 'omninode_internal', 'omninode_cloud']
  LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
      EXECUTE format('CREATE ROLE %I WITH NOLOGIN NOSUPERUSER NOBYPASSRLS', r);
    END IF;
  END LOOP;
END$$;
"""


def _exposures() -> dict[str, tuple[ProjectionTableConfig, ...]]:
    """Node name -> its exposures that declare a freshness column."""
    found: dict[str, tuple[ProjectionTableConfig, ...]] = {}
    for contract_path in sorted(_NODES.glob("*/contract.yaml")):
        data = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        configs = tuple(
            cfg
            for cfg in load_projection_exposures_from_contract(
                data, contract_path.parent.name, contract_path
            )
            if cfg.freshness_column
        )
        if configs:
            found[contract_path.parent.name] = configs
    return found


_EXPOSURES = _exposures()


def test_the_walk_finds_every_contract_declaring_a_freshness_column() -> None:
    declaring = {
        p.parent.name
        for p in _NODES.glob("*/contract.yaml")
        if "freshness_column" in p.read_text(encoding="utf-8")
    }
    # Some contracts mention the key in prose or only in a non-exposed block.
    assert set(_EXPOSURES) <= declaring
    assert len(_EXPOSURES) == 61, sorted(_EXPOSURES)


def _creates(node: str, table: str) -> bool:
    pattern = re.compile(
        rf"CREATE\s+TABLE\s+(IF\s+NOT\s+EXISTS\s+)?([\w\"]+\.)?\"?{re.escape(table)}\"?\b",
        re.IGNORECASE,
    )
    return any(
        pattern.search(m.read_text(encoding="utf-8"))
        for m in (_NODES / node / "migrations").glob("*.sql")
    )


def _migration_chain(node: str, tables: tuple[str, ...]) -> list[Path]:
    owners = [*_READS_FROM.get(node, ()), node]
    for table in tables:
        if not _creates(node, table):
            owners = [
                p.parent.name
                for p in sorted(_NODES.glob("*/migrations"))
                if _creates(p.parent.name, table)
            ] + owners
    chain: list[Path] = []
    for owner in dict.fromkeys(owners):
        chain.extend(sorted((_NODES / owner / "migrations").glob("*.sql")))
    return chain


@asynccontextmanager
async def _provisioned(
    pg: _Postgres, node: str, tables: tuple[str, ...]
) -> AsyncIterator[str]:
    """A throwaway schema holding the node's own migration chain.

    A table another node's migrations create (``llm_call_metrics`` belongs to
    ``node_projection_llm_cost``) is built from that owner's chain, applied first.
    """
    admin = await asyncpg.connect(**pg.connect_kwargs())
    schema = f"omn24562_{uuid4().hex[:12]}"
    try:
        await admin.execute(_ROLES_SQL)
        await admin.execute(_EXTRA_ROLES_SQL)
        await admin.execute(f"CREATE SCHEMA {schema}")
        await admin.execute(f"SET search_path TO {schema}, public")
        registry = (
            _NODES
            / "node_projection_tenant_registry/migrations"
            / "0000_create_tenant_registry_mirror.sql"
        )
        await admin.execute(registry.read_text(encoding="utf-8"))
        for migration in _migration_chain(node, tables):
            sql = (
                migration.read_text(encoding="utf-8")
                .replace("CREATE INDEX CONCURRENTLY", "CREATE INDEX")
                .replace("omninode_internal.", f"{schema}.")
                .replace("SCHEMA omninode_internal", f"SCHEMA {schema}")
                .replace("'omninode_internal'", f"'{schema}'")
                .replace("table_schema = 'public'", f"table_schema = '{schema}'")
                .replace("public.", f"{schema}.")
            )
            await admin.execute(sql)
        yield schema
    finally:
        with contextlib.suppress(asyncpg.PostgresError):
            await admin.execute("SET search_path TO public")
            await admin.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        await admin.close()


async def _windowed_reads(
    pg: _Postgres, schema: str, cfg: ProjectionTableConfig
) -> dict[str, tuple[int, object]]:
    # An exposure not yet converted to bus_backed is refused before any read
    # (not_yet_bus_backed); the windowed read it will answer once converted is the
    # one walked here, so the flag is set and the read goes through the same path.
    scoped = cfg.model_copy(update={"relation_schema": schema, "bus_backed": True})
    answers: dict[str, tuple[int, object]] = {}
    for window in _WINDOWS:
        source = TableRowSource.for_database_url(pg.dsn(schema))
        try:
            page = await read_projection_page(
                cfg.topic,
                topic_map={cfg.topic: scoped},
                source=source,
                tenant=_TENANT if cfg.tenant_column else None,
                window=window,
            )
        finally:
            await source.close()
        answers[window] = (page.status_code, page.body)
    return answers


@pytest.mark.asyncio
@pytest.mark.parametrize("node", sorted(_EXPOSURES))
async def test_every_exposure_answers_the_1d_7d_and_30d_windowed_read(
    postgres: _Postgres, node: str
) -> None:
    tables = tuple(cfg.table for cfg in _EXPOSURES[node])
    async with _provisioned(postgres, node, tables) as schema:
        for cfg in _EXPOSURES[node]:
            answers = await _windowed_reads(postgres, schema, cfg)
            for window, (status, body) in answers.items():
                assert status == 200, (cfg.topic, window, status, body)


# The window expression as it stood before OMN-21288 (commit e5f2afdf3d): the
# freshness column is compared as it is, whatever its type.
def _pre_omn21288_instant(quoted: str, column_type: str | None) -> str:
    return quoted


@pytest.mark.asyncio
async def test_planted_control_a_text_freshness_column_fails_the_walk(
    postgres: _Postgres, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Planted control: the pre-OMN-21288 expression 503s the metering exposure."""
    node = "node_projection_metering_summary"
    cfg = _EXPOSURES[node][0]
    from omnimarket.handlers import projection_window_instant

    async with _provisioned(postgres, node, (cfg.table,)) as schema:
        admin = await asyncpg.connect(**postgres.connect_kwargs())
        try:
            column_type = await projection_window_instant.freshness_column_type(
                admin, f"{schema}.{cfg.table}", cfg.freshness_column or ""
            )
        finally:
            await admin.close()
        assert projection_window_instant.is_text_type(column_type), column_type
        for module in _expression_users():
            monkeypatch.setattr(module, "window_instant_sql", _pre_omn21288_instant)
        answers = await _windowed_reads(postgres, schema, cfg)
    assert {status for status, _ in answers.values()} == {503}, answers


def _expression_users() -> list[object]:
    from omnimarket.projection import table_reader

    return [table_reader]
