# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20010: migration 0004 nulls hashed span model, description and workflow_phase.

A span written while the emit drainer hashed lineage.agent_model and
lineage.agent_description holds 'sha256:<hex>' in those columns. The writer keeps
the first non-null value, so the hash would block the plain value forever. The
migration sets the hashed values to NULL and leaves every plain value alone.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import quote_plus

import asyncpg
import pytest

_NODE_DIR = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_projection_claude_hook_events"
)
_MIGRATION_0004 = _NODE_DIR / "migrations" / "0004_null_hashed_span_lineage.sql"
_BASE = (
    _NODE_DIR / "migrations" / "0000_create_claude_hook_events.sql",
    _NODE_DIR / "migrations" / "0003_add_span_model_and_description.sql",
)
_SCHEMA = "omn20010_null_hashed_span_lineage_test"
_HASH = "sha256:" + "ab" * 32


@pytest.mark.unit
def test_migration_is_static_sql_the_application_gate_accepts() -> None:
    text = _MIGRATION_0004.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("--")
    )
    assert not re.search(r"\bDO\s+\$", code, re.IGNORECASE)
    assert not re.search(r"\bEXECUTE\b", code, re.IGNORECASE)
    assert len(re.findall(r"^UPDATE\s", code, re.MULTILINE)) == 3
    assert "omninode_internal.claude_agent_spans" in code


def _scoped(statement: str) -> str:
    return statement.replace("omninode_internal.", f"{_SCHEMA}.").replace(
        "'omninode_internal'", f"'{_SCHEMA}'"
    )


async def _connect_or_skip() -> asyncpg.Connection:
    password = os.environ.get(
        "INTEGRATION_POSTGRES_PASSWORD", os.environ.get("POSTGRES_PASSWORD", "")
    )
    if not password:
        pytest.skip("INTEGRATION_POSTGRES_PASSWORD not set -- skipping migration proof")
    host = os.environ.get("INTEGRATION_POSTGRES_HOST", "localhost")
    port = int(os.environ.get("INTEGRATION_POSTGRES_PORT", "5432"))
    user = os.environ.get("INTEGRATION_POSTGRES_USER", "postgres")
    db = os.environ.get("INTEGRATION_POSTGRES_DB", "omnibase_infra")
    dsn = f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{db}"
    try:
        return await asyncpg.connect(dsn)
    except (OSError, asyncpg.PostgresError) as exc:  # pragma: no cover - infra
        pytest.skip(f"no reachable Postgres for the migration proof: {exc}")


@pytest.mark.integration
async def test_hashed_values_are_nulled_and_plain_values_survive() -> None:
    conn = await _connect_or_skip()
    try:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
        for migration in _BASE:
            await conn.execute(_scoped(migration.read_text(encoding="utf-8")))
        rows = (
            ("s", "hashed-both", _HASH, _HASH, _HASH),
            ("s", "hashed-model", _HASH, "plain task", "Read"),
            ("s", "plain-both", "sonnet", "plain task", "Read"),
            ("s", "absent", None, None, None),
        )
        for session_id, agent_id, model, description, phase in rows:
            await conn.execute(
                f"INSERT INTO {_SCHEMA}.claude_agent_spans "
                "(session_id, agent_id, parent_resolution, started_at, model, description, workflow_phase) "
                "VALUES ($1, $2, 'unknown', NOW(), $3, $4, $5)",
                session_id,
                agent_id,
                model,
                description,
                phase,
            )
        migration_sql = _scoped(_MIGRATION_0004.read_text(encoding="utf-8"))
        await conn.execute(migration_sql)
        await conn.execute(migration_sql)  # a re-run matches nothing and does not error
        got = {
            r["agent_id"]: (r["model"], r["description"], r["workflow_phase"])
            for r in await conn.fetch(
                f"SELECT agent_id, model, description, workflow_phase FROM {_SCHEMA}.claude_agent_spans"
            )
        }
        assert got == {
            "hashed-both": (None, None, None),
            "hashed-model": (None, "plain task", "Read"),
            "plain-both": ("sonnet", "plain task", "Read"),
            "absent": (None, None, None),
        }
    finally:
        await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
        await conn.close()
