# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Real-Postgres proof that delegate-skill rows store their routing tier (OMN-20755).

The unit module proves the rule on a SQLite store. It cannot prove that the
deployed async writer's statement and the local sync writer both bind
``cost_tier_name`` into Postgres's ``TEXT NOT NULL DEFAULT ''`` column
(migration 0018), or that a terminal with no attempts leaves the stored tier
alone there. Here the node's whole migration chain is applied to a throwaway
PostgreSQL 16 schema and both writers write delegate-skill terminals.

THE RED HALF IS MECHANICAL. Before the change neither writer names the column,
so every row stores the default ``''`` and each tier assertion fails.

WHICH DATABASE. The same as tests/test_omn19860_caller_lane_projection_real_postgres.py,
whose fixtures this module reuses: CI's INTEGRATION_POSTGRES service when
``INTEGRATION_POSTGRES_PASSWORD`` is set, else an owned localhost-only
container, skipping when docker is absent.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import asyncpg
import pytest

import tests.test_omn19860_caller_lane_projection_real_postgres as _pg
from omnimarket.models.delegation.wire.model_delegate_skill_terminal_projection import (
    ModelDelegateSkillTerminalProjection,
)
from omnimarket.nodes.node_projection_delegation.handlers.handler_projection_delegation import (
    HandlerProjectionDelegation,
)
from omnimarket.projection.postgres_sync_database import PostgresSyncProjectionAdapter
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.integration

# The caller-lane module's database fixture and helpers, reused rather than
# copied: one place decides which PostgreSQL 16 these real-DB tests reach.
postgres = _pg.postgres
_NullPublisher = _pg._NullPublisher
_payload = _pg._payload
_Postgres = _pg._Postgres
_provisioned = _pg._provisioned
_runner = _pg._runner


def _attempt(
    tier: str, *, passed: bool = True, failure: str | None = None
) -> dict[str, Any]:
    return {
        "tier": tier,
        "backend_id": f"{tier}-backend",
        "model_id": "gemini-3.5-flash-lite",
        "quality_gate_passed": passed,
        "failure_class": failure,
    }


async def _stored_tier(admin: asyncpg.Connection, correlation_id: str) -> object:
    row = await admin.fetchrow(
        "SELECT cost_tier_name FROM delegation_events WHERE correlation_id = $1",
        correlation_id,
    )
    assert row is not None, f"no delegation_events row for {correlation_id}"
    return row["cost_tier_name"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_deployed_async_writer_stores_the_serving_tier_and_keeps_it(
    postgres: _Postgres,
) -> None:
    accepted, escalated = str(uuid4()), str(uuid4())
    async with (
        _provisioned(postgres) as (admin, schema),
        _runner(postgres, schema) as runner,
    ):
        meta = MessageMeta(partition=0, offset=1, fallback_id=accepted)
        assert await runner._project_delegate_skill_terminal(
            _payload(accepted, attempts=[_attempt("cheap_frontier")]), meta
        )
        assert await _stored_tier(admin, accepted) == "cheap_frontier"
        # The same correlation re-emitted with no attempts: the tier is kept.
        await runner._project_delegate_skill_terminal(_payload(accepted), meta)
        assert await _stored_tier(admin, accepted) == "cheap_frontier"
        # An escalated run stores the rung that answered.
        assert await runner._project_delegate_skill_terminal(
            _payload(
                escalated,
                attempts=[
                    _attempt("local", passed=False, failure="quality_gate_failed"),
                    _attempt("cheap_frontier"),
                ],
            ),
            MessageMeta(partition=0, offset=2, fallback_id=escalated),
        )
        assert await _stored_tier(admin, escalated) == "cheap_frontier"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_local_sync_writer_stores_the_serving_tier(postgres: _Postgres) -> None:
    corr = str(uuid4())
    async with _provisioned(postgres) as (admin, schema):
        adapter = PostgresSyncProjectionAdapter(postgres.dsn(schema))
        try:
            HandlerProjectionDelegation(
                publisher=_NullPublisher()
            ).project_delegate_skill_terminal(
                ModelDelegateSkillTerminalProjection.from_payload(
                    _payload(corr, attempts=[_attempt("cheap_frontier")])
                ),
                adapter,
            )
        finally:
            close = getattr(adapter, "close", None)
            if callable(close):
                close()
        assert await _stored_tier(admin, corr) == "cheap_frontier"
