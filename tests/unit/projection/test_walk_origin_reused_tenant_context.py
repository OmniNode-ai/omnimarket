# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""walk_origin on a reused pooled connection must set the tenant GUC first."""

from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import asyncpg
import pytest

from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.table_reader import (
    ProjectionReadError,
    TableRowSource,
)
from omnimarket.projection.tenant_isolation import TENANT_GUC

pytestmark = pytest.mark.unit


class ReusedConnection:
    """A pooled session after SET LOCAL has committed and left an empty GUC."""

    def __init__(self, *, min_cursor: int | None = 42) -> None:
        self.tenant = ""
        self.readonly = False
        self._min_cursor = min_cursor

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
        return "SELECT 0"

    async def fetchval(self, sql: str, *params: Any) -> Any:
        # The live delegation_eval_results policy casts this GUC to uuid;
        # Postgres can evaluate that cast even on a min() read.
        try:
            UUID(self.tenant)
        except ValueError as exc:
            raise asyncpg.InvalidTextRepresentationError(
                'invalid input syntax for type uuid: ""'
            ) from exc
        assert self.readonly
        return self._min_cursor


class ReusedPool:
    def __init__(self, *, min_cursor: int | None = 42) -> None:
        self.connection = ReusedConnection(min_cursor=min_cursor)

    @asynccontextmanager
    async def acquire(self) -> Any:
        yield self.connection


def _config() -> ProjectionTableConfig:
    return ProjectionTableConfig(
        topic="onex.snapshot.projection.delegation.acceptance-eval.v1",
        table="delegation_eval_results",
        schema_name="public",
        relation_schema="public",
        columns=("tenant_id", "eval_run_id"),
        key_columns=("tenant_id", "eval_run_id"),
        tenant_column="tenant_id",
        cursor_column="projection_cursor",
        bus_backed=True,
    )


async def test_walk_origin_sets_the_tenant_guc_on_a_reused_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _config()
    pool = ReusedPool(min_cursor=42)
    source = TableRowSource()

    async def acquire_pool(config: ProjectionTableConfig) -> Any:
        return pool

    monkeypatch.setattr(source, "_pool", acquire_pool)
    tenant = str(UUID(int=0x11223344556677889900AABBCCDDEEFF))
    for _ in range(2):
        origin = await source.walk_origin(cfg, tenant_id=tenant)
        assert origin == "41"
        assert pool.connection.tenant == ""


async def test_walk_origin_refuses_a_tenant_that_is_not_a_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _config()
    pool = ReusedPool(min_cursor=42)
    source = TableRowSource()

    async def acquire_pool(config: ProjectionTableConfig) -> Any:
        return pool

    monkeypatch.setattr(source, "_pool", acquire_pool)
    with pytest.raises(ProjectionReadError) as excinfo:
        await source.walk_origin(cfg, tenant_id="not-a-uuid")
    assert excinfo.value.code == "projection_database_unavailable"
