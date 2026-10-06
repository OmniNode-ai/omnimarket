# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-20006: readiness after a scoped read must survive asyncpg's GUC reset."""

from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import asyncpg
import pytest

from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.table_reader import TableRowSource
from omnimarket.projection.tenant_isolation import TENANT_GUC

pytestmark = pytest.mark.unit


class ReusedConnection:
    """A pooled session after SET LOCAL has committed and left an empty GUC."""

    def __init__(self) -> None:
        self.tenant = ""
        self.readonly = False
        self.probes: list[str] = []

    @asynccontextmanager
    async def transaction(self, *, readonly: bool = False) -> Any:
        before = self.tenant
        self.readonly = readonly
        try:
            yield
        finally:
            self.tenant = before
            self.readonly = False

    async def execute(self, sql: str, *params: Any) -> str:
        if params and params[0] == TENANT_GUC:
            self.tenant = params[1]
            return "SELECT 1"
        # The live delegation_eval_results policy casts this GUC to uuid;
        # Postgres can evaluate that cast even on a LIMIT 0 probe.
        try:
            UUID(self.tenant)
        except ValueError as exc:
            raise asyncpg.InvalidTextRepresentationError(
                'invalid input syntax for type uuid: ""'
            ) from exc
        assert self.readonly
        self.probes.append(sql)
        return "SELECT 0"


class ReusedPool:
    def __init__(self) -> None:
        self.connection = ReusedConnection()

    @asynccontextmanager
    async def acquire(self) -> Any:
        yield self.connection


async def test_readiness_rechecks_the_relation_on_a_reused_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = ProjectionTableConfig(
        topic="onex.snapshot.projection.delegation.acceptance-eval.v1",
        table="delegation_eval_results",
        schema_name="public",
        relation_schema="public",
        columns=("tenant_id", "eval_run_id"),
        key_columns=("tenant_id", "eval_run_id"),
        tenant_column="tenant_id",
        bus_backed=True,
    )
    pool = ReusedPool()
    source = TableRowSource()

    async def acquire_pool(config: ProjectionTableConfig) -> Any:
        return pool

    monkeypatch.setattr(source, "_pool", acquire_pool)
    for _ in range(2):
        ready, result = await source.readiness({cfg.topic: cfg})
        assert ready, result
        assert result["served_topics"] == {cfg.topic: True}
        assert pool.connection.tenant == ""

    assert len(pool.connection.probes) == 2
    assert all(
        '"tenant_id", "eval_run_id" FROM "public"."delegation_eval_results" LIMIT 0'
        in sql
        for sql in pool.connection.probes
    )
