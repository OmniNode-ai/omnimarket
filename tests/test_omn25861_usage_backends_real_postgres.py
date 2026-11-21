# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-25861 on real Postgres: migration 0004 and the writer's per-call statements serve each row's backends.

The local store's cases are tests/projection/test_omn25861_usage_backends.py; these are their Postgres twins, run
through the writer's own write_usage_call on a throwaway schema. It connects through the INTEGRATION_POSTGRES_* DSN
(the omn19978 real-Postgres helper) and skips without a reachable server, so a skip here proves nothing.

0004 is authored here and vendored into omnibase_infra after this merges (the spec's D2), so for a while a lane's
database is a step behind the code that writes to it. The writer follows the columns in force, as 0003's writer
follows the key in force.

Failure modes:
R1  two calls to one model, one through byok-reflection, do not give the row exactly ["byok-reflection"] (AC1);
R2  a direct call is listed, a backend is listed twice or out of order, or a row counting a call with no stated
    backend serves a list instead of NULL (the store's B2 to B4, on the writer's SQL);
R3  0004 rewrites or drops a row stored before it, or cannot run twice;
R4  the writer raises, or loses the call, on a database 0004 has not reached;
R5  the rows the writer returns for the bus snapshot lack the backends, or the Postgres read serves them as text
    rather than a list;
R6  under the three-column key 0003 keeps, the model's day folded into one not_tier_routed row does not list the
    backends of every tier's calls;
R7  the database's collation orders the list differently from the local store's code-point sort;
R8  with 0004 half applied (its calls-table ALTER committed, its day-table ALTER not yet), a stated backend is not
    stored, so the row serves NULL for good once the day column arrives.

Each case states which key is in force: 0003 keeps the three-column key and the contract migration (OMN-21161,
copied below as tests/test_omn21015_usage_tier_share_real_postgres.py copies it) makes the tier part of it.
"""

from __future__ import annotations

import json
from pathlib import Path

import asyncpg
import pytest
import yaml

from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_projection_usage_by_model_day import (
    HandlerProjectionUsageByModelDay,
)
from omnimarket.nodes.node_projection_usage_by_model_day.handlers.handler_usage_by_model_day_writer import (
    write_usage_call,
)
from omnimarket.nodes.node_projection_usage_by_model_day.models import (
    ModelUsageCallEvent,
)
from omnimarket.projection.discovery import load_projection_exposures_from_contract
from omnimarket.projection.table_reader import serialise_row
from tests.test_omn19978_usage_by_model_day_real_postgres import (
    _MIGRATIONS,
    _connect_or_skip,
)

_SCHEMA = "omn25861_usage_backends_test"
_NODE = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_usage_by_model_day"
)
_BACKENDS_MIGRATION = _NODE / "migrations/0004_usage_by_model_day_backends.sql"
_BEAM = "Beam-1.0-501B-A23B"
_UNSET = object()


def _scoped(statement: str) -> str:
    return statement.replace("public.", f"{_SCHEMA}.")


# What the contract migration (OMN-21161) does: the four-column index becomes the key.
_TIER_KEY = f"""
    ALTER TABLE {_SCHEMA}.usage_by_model_day DROP CONSTRAINT usage_by_model_day_pkey;
    ALTER TABLE {_SCHEMA}.usage_by_model_day
        ADD CONSTRAINT usage_by_model_day_pkey PRIMARY KEY USING INDEX usage_by_model_day_tier_key;
"""


async def _fresh(
    conn: asyncpg.Connection, *, with_backends: bool = True, tier_key: bool = True
) -> None:
    await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await conn.execute(f"CREATE SCHEMA {_SCHEMA}")
    for migration in _MIGRATIONS:
        await conn.execute(_scoped(migration.read_text(encoding="utf-8")))
    if tier_key:
        await conn.execute(_TIER_KEY)
    if with_backends:
        await conn.execute(_scoped(_BACKENDS_MIGRATION.read_text(encoding="utf-8")))


async def _drop(conn: asyncpg.Connection) -> None:
    await conn.execute(f"DROP SCHEMA IF EXISTS {_SCHEMA} CASCADE")
    await conn.close()


async def _write(
    conn: asyncpg.Connection,
    call_id: str,
    backend: object = _UNSET,
    *,
    model: str = _BEAM,
    tier: str = "cheap_cloud",
):
    fields: dict[str, object] = {
        "call_id": call_id,
        "model_name": model,
        "tenant_id": "t",
        "cost_tier_name": tier,
        "timestamp": "2026-11-19T05:00:00Z",
        "prompt_tokens": 1,
        "completion_tokens": 1,
        "estimated_cost_usd": 0.0,
        "usage_source": "measured",
    }
    if backend is not _UNSET:
        fields["backend"] = backend
    delta = HandlerProjectionUsageByModelDay().handle(
        ModelUsageCallEvent.model_validate(fields)
    )
    async with conn.transaction():
        return await write_usage_call(conn, delta, schema=_SCHEMA)


async def _backends(conn: asyncpg.Connection) -> dict[tuple[str, str], object]:
    """Each row's backends, read as the projection API reads and serialises them."""
    contract = _NODE / "contract.yaml"
    (cfg,) = load_projection_exposures_from_contract(
        yaml.safe_load(contract.read_text(encoding="utf-8")),
        "node_projection_usage_by_model_day",
        contract,
    )
    rows = await conn.fetch(
        f"SELECT model_id, cost_tier_name, backends FROM {_SCHEMA}.usage_by_model_day"
    )
    return {
        (str(r["model_id"]), str(r["cost_tier_name"])): serialise_row(cfg, r)[
            "backends"
        ]
        for r in rows
    }


@pytest.mark.integration
async def test_real_postgres_a_model_reached_through_byok_reflection_serves_it() -> (
    None
):
    """R1, AC1's falsifier on the writer's SQL: two calls to one model, one through byok-reflection."""
    conn = await _connect_or_skip()
    try:
        await _fresh(conn)
        await _write(conn, "c1", "byok-reflection")
        written = await _write(conn, "c2", _BEAM)
        assert written is not None
        # R5: the row published to the bus snapshot carries the backends.
        (published,) = [dict(r) for r in written.rows]
        assert published["model_id"] == _BEAM
        assert published["call_count"] == 2
        backends = published["backends"]
        # asyncpg returns JSONB as its text; the snapshot publisher decodes it (json_columns).
        assert (json.loads(backends) if isinstance(backends, str) else backends) == [
            "byok-reflection"
        ]
        assert await _backends(conn) == {(_BEAM, "cheap_cloud"): ["byok-reflection"]}
    finally:
        await _drop(conn)


@pytest.mark.integration
async def test_real_postgres_direct_duplicate_and_unstated_backends() -> None:
    """R2: the store's rule on the writer's SQL."""
    conn = await _connect_or_skip()
    try:
        await _fresh(conn)
        await _write(conn, "d1", _BEAM, model=_BEAM, tier="local")
        await _write(conn, "d2", "", model=_BEAM, tier="local")
        for call_id, backend in (
            ("s1", "byok-reflection"),
            ("s2", "byok-openrouter"),
            ("s3", "byok-reflection"),
        ):
            await _write(conn, call_id, backend)
        await _write(conn, "u1", "byok-reflection", model="qwen")
        await _write(conn, "u2", model="qwen")
        assert await _backends(conn) == {
            (_BEAM, "local"): [],
            (_BEAM, "cheap_cloud"): ["byok-openrouter", "byok-reflection"],
            ("qwen", "cheap_cloud"): None,
        }
    finally:
        await _drop(conn)


@pytest.mark.integration
async def test_real_postgres_0004_keeps_old_rows_and_runs_twice() -> None:
    """R3: a row stored before 0004 is kept, with its backends NULL (never recorded), and 0004 is re-runnable."""
    conn = await _connect_or_skip()
    try:
        await _fresh(conn, with_backends=False)
        await _write(conn, "old")
        before = await conn.fetch(
            f"SELECT tenant_id, usage_day, model_id, cost_tier_name, input_tokens, "
            f"output_tokens, call_count FROM {_SCHEMA}.usage_by_model_day"
        )
        migration = _scoped(_BACKENDS_MIGRATION.read_text(encoding="utf-8"))
        await conn.execute(migration)
        await conn.execute(migration)
        after = await conn.fetch(
            f"SELECT tenant_id, usage_day, model_id, cost_tier_name, input_tokens, "
            f"output_tokens, call_count FROM {_SCHEMA}.usage_by_model_day"
        )
        assert [dict(r) for r in after] == [dict(r) for r in before]
        assert await _backends(conn) == {(_BEAM, "cheap_cloud"): None}
        (stored,) = await conn.fetch(
            f"SELECT backend FROM {_SCHEMA}.usage_by_model_day_calls"
        )
        assert stored["backend"] is None
    finally:
        await _drop(conn)


@pytest.mark.integration
async def test_real_postgres_the_writer_keeps_writing_before_0004_reaches_the_database() -> (
    None
):
    """R4: on a database at 0003 the new writer stores the call and counts it, and raises nothing."""
    conn = await _connect_or_skip()
    try:
        await _fresh(conn, with_backends=False)
        written = await _write(conn, "early", "byok-reflection")
        assert written is not None
        (row,) = [dict(r) for r in written.rows]
        assert row["call_count"] == 1
        assert "backends" not in row
        (stored,) = await conn.fetch(
            f"SELECT call_id FROM {_SCHEMA}.usage_by_model_day_calls"
        )
        assert stored["call_id"] == "early"
    finally:
        await _drop(conn)


@pytest.mark.integration
async def test_real_postgres_under_the_old_key_the_folded_row_lists_every_tiers_backends() -> (
    None
):
    """R6: 0003's three-column key folds a model's day into one not_tier_routed row; it lists both tiers' backends."""
    conn = await _connect_or_skip()
    try:
        await _fresh(conn, tier_key=False)
        await _write(conn, "o1", "byok-reflection", tier="cheap_cloud")
        await _write(conn, "o2", "byok-openrouter", tier="local")
        await _write(conn, "o3", _BEAM, tier="local")
        assert await _backends(conn) == {
            (_BEAM, "not_tier_routed"): ["byok-openrouter", "byok-reflection"]
        }
    finally:
        await _drop(conn)


@pytest.mark.integration
async def test_real_postgres_backends_sort_by_code_point_as_the_local_store_does() -> (
    None
):
    """R7: "cloud-glm" sorts before "cloudflare-x" by code point; a linguistic collation that skips punctuation does not."""
    conn = await _connect_or_skip()
    try:
        await _fresh(conn)
        await _write(conn, "x1", "cloudflare-x")
        await _write(conn, "x2", "cloud-glm")
        assert await _backends(conn) == {
            (_BEAM, "cheap_cloud"): ["cloud-glm", "cloudflare-x"]
        }
    finally:
        await _drop(conn)


@pytest.mark.integration
async def test_real_postgres_a_half_applied_0004_still_stores_the_stated_backend() -> (
    None
):
    """R8: 0004's two ALTERs commit separately; the call stored between them keeps its backend."""
    conn = await _connect_or_skip()
    try:
        await _fresh(conn, with_backends=False)
        body = "\n".join(
            line
            for line in _scoped(
                _BACKENDS_MIGRATION.read_text(encoding="utf-8")
            ).splitlines()
            if not line.lstrip().startswith("--")
        )
        first, second = [part for part in body.split(";") if part.strip()]
        await conn.execute(first)
        written = await _write(conn, "h1", "byok-reflection")
        assert written is not None
        (stored,) = await conn.fetch(
            f"SELECT backend FROM {_SCHEMA}.usage_by_model_day_calls"
        )
        assert stored["backend"] == "byok-reflection"
        await conn.execute(second)
        await _write(conn, "h2", _BEAM)
        assert await _backends(conn) == {(_BEAM, "cheap_cloud"): ["byok-reflection"]}
    finally:
        await _drop(conn)
