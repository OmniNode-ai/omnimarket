# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Prepared real-PG controls; collection or missing-service skips are not proof.

Execute only through the governed CI PostgreSQL fixture. No driver or migration
implementation is justified until these cases have actually run RED there.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import asyncpg
import psycopg2.errors
import pytest

from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_store import (
    apply_credential_registered,
)
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter
from omnimarket.projection.runner import MessageMeta
from tests.test_omn16316_real_postgres_tenant_credentials_write_path import (
    BYOK_TENANT,
    TOPIC_REGISTERED,
    _base_dsn,
    _provisioned_runner,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _list(value: Any) -> Any:
    return json.loads(value) if isinstance(value, str) else value


def _writer(runner: Any, schema: str, driver: str) -> Any:
    offset = 0

    async def register(ref: str, models: list[str], version: int | None = None) -> bool:
        nonlocal offset
        payload = {
            "tenant_id": BYOK_TENANT,
            "provider": "openrouter",
            "name": ref,
            "api_key_ref": ref,
            "models": models,
            "expected_version": version,
        }
        if driver == "sync":
            return await asyncio.to_thread(
                apply_credential_registered,
                payload,
                PostgresSyncProjectionAdapter(_base_dsn(), schema=schema),
            )
        offset += 1
        return await runner.project_event(
            TOPIC_REGISTERED,
            payload,
            MessageMeta(
                partition=0,
                offset=offset,
                fallback_id=f"per-key-{offset}",
                topic=TOPIC_REGISTERED,
            ),
        )

    return register


@pytest.mark.parametrize("driver", ["sync", "async"])
async def test_real_postgres_two_key_lists_and_global_stale_cas(driver: str) -> None:
    async with _provisioned_runner() as (runner, conn, schema):
        register = _writer(runner, schema, driver)
        a, b = "cred_test_openrouter_a", "cred_test_openrouter_b"
        assert await register(a, ["a-first", "a-next"])
        assert await register(b, ["b-first", "b-next"])
        assert await register(a, ["a-replaced", "a-new-tail"], 2)
        # Processed/ACK True is deliberately retained for a stale replace.
        assert await register(b, ["stale-must-not-publish"], 2)
        rows = await conn.fetch(
            "SELECT api_key_ref, models FROM tenant_inference_credentials WHERE tenant_id=$1",
            BYOK_TENANT,
        )
        assert {r["api_key_ref"]: _list(r["models"]) for r in rows} == {
            a: ["a-replaced", "a-new-tail"],
            b: ["b-first", "b-next"],
        }
        route = await conn.fetchrow(
            "SELECT models, version, secret_ref FROM delegation_routing_tenant_overlay WHERE tenant_id=$1 AND task_type='*'",
            BYOK_TENANT,
        )
        assert route is not None
        assert route["version"] == 3
        assert route["secret_ref"] == a
        assert _list(route["models"]) == ["a-replaced", "a-new-tail"]


@pytest.mark.parametrize("driver", ["sync", "async"])
async def test_real_postgres_catalogue_error_rolls_back_overlay_and_models(
    driver: str,
) -> None:
    async with _provisioned_runner() as (runner, conn, schema):
        register = _writer(runner, schema, driver)
        ref = "cred_test_openrouter_rollback"
        assert await register(ref, ["before", "before-tail"])
        # Arm the fault before production has a models column. The separate
        # two-key test requires that production itself provides this column.
        await conn.execute(
            "ALTER TABLE tenant_inference_credentials ADD COLUMN IF NOT EXISTS models JSONB"
        )
        await conn.execute(
            "UPDATE tenant_inference_credentials SET models=$1::JSONB WHERE api_key_ref=$2",
            json.dumps(["before", "before-tail"]),
            ref,
        )
        await conn.execute("""
            CREATE FUNCTION refuse_chosen_models() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN RAISE EXCEPTION 'chosen-model-write-fault'; END $$;
            CREATE TRIGGER refuse_chosen_models BEFORE UPDATE OF models
            ON tenant_inference_credentials FOR EACH ROW EXECUTE FUNCTION refuse_chosen_models();
        """)
        with pytest.raises(asyncpg.RaiseError, match="chosen-model-write-fault"):
            await conn.execute(
                "UPDATE tenant_inference_credentials SET models='[]'::JSONB WHERE api_key_ref=$1",
                ref,
            )
        before_route = dict(
            await conn.fetchrow(
                "SELECT * FROM delegation_routing_tenant_overlay WHERE tenant_id=$1",
                BYOK_TENANT,
            )
        )
        before_key = _list(
            await conn.fetchval(
                "SELECT models FROM tenant_inference_credentials WHERE api_key_ref=$1",
                ref,
            )
        )
        with pytest.raises(
            (asyncpg.RaiseError, psycopg2.errors.RaiseException),
            match="chosen-model-write-fault",
        ):
            await register(ref, ["after", "after-tail"], 1)
        after_route = dict(
            await conn.fetchrow(
                "SELECT * FROM delegation_routing_tenant_overlay WHERE tenant_id=$1",
                BYOK_TENANT,
            )
        )
        assert after_route == before_route
        assert (
            _list(
                await conn.fetchval(
                    "SELECT models FROM tenant_inference_credentials WHERE api_key_ref=$1",
                    ref,
                )
            )
            == before_key
        )


_NODES = Path(__file__).resolve().parents[1] / "src" / "omnimarket" / "nodes"
_REPLAYED = {
    "0007": (
        _NODES
        / "node_delegation_routing_reducer/migrations"
        / "0007_add_credential_ordered_models.sql",
        ("credential_route_version_positive", "credential_route_models_array"),
    ),
    "0009": (
        _NODES
        / "node_projection_tenant_credentials/migrations"
        / "0009_tenant_credentials_models_and_version.sql",
        (
            "tenant_inference_credentials_models_array",
            "tenant_inference_credentials_version_positive",
        ),
    ),
}


@pytest.mark.parametrize("migration", sorted(_REPLAYED))
async def test_real_postgres_ordered_model_migrations_replay_cleanly(
    migration: str,
) -> None:
    """A replayed or partly applied migration must not fail on its constraints."""
    path, constraints = _REPLAYED[migration]
    async with _provisioned_runner() as (_runner, conn, schema):
        await conn.execute(path.read_text())
        rows = await conn.fetch(
            """
            SELECT c.conname
            FROM pg_constraint c
            JOIN pg_namespace n ON n.oid = c.connamespace
            WHERE n.nspname = $1 AND c.conname = ANY($2::text[])
            """,
            schema,
            list(constraints),
        )
        assert sorted(row["conname"] for row in rows) == sorted(constraints)
