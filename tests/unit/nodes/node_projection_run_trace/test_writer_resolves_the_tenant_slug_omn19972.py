# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19972: the run-trace writer resolves a step's tenant through the tenant mirror.

On a bundle, the deployed dispatch port publishes run steps carrying the
install's tenant slug, while every reader scopes by the tenant uuid, so rows
stored under the slug were never served (omnibase_infra#5041's lab run: 5 rows
folded, 0 served). The ruling (OMN-19972 c2939bce) is one rule for every
projection writer: resolve on write, as the delegation writer does, through the
shared ``async_resolve_write_tenant_uuid``.

These tests drive the writer's own ``_project_step`` against a stand-in pool
that keeps rows by key and answers the mirror's lookups, so they need no
database. The real-Postgres twin is in
``tests/test_omn19972_run_trace_tenant_real_postgres.py``.

Failure modes each test is written against:

* (F1) the slug is stored, so a uuid-scoped read never serves the row;
* (F2) an unknown slug is stored, or stamped with the house or a default
  tenant, instead of refused to the poison path;
* (F3) a uuid the mirror holds is altered, or one it does not hold is stored;
* (F4) a redelivered slug step makes a second row, or publishes a row other
  than the stored one;
* (F5) the session's tenant and the row's tenant disagree, so row-level
  security would refuse the insert.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from omnimarket.nodes.node_projection_run_trace.handlers.handler_run_trace_writer import (
    RunTraceProjectionWriter,
)
from omnimarket.projection.runner import MessageMeta
from omnimarket.projection.tenant_registry_resolution import (
    TenantRegistryResolutionError,
)

_CHAIN = json.loads(
    (
        Path(__file__).resolve().parents[3] / "fixtures/omn19987/run_step_chain.json"
    ).read_text(encoding="utf-8")
)
_SLUG = "local-ee67b16e7aab"
_UUID = "97b9aa50-f53f-adb8-5404-5c7318dd4cb1"
_COLUMNS = (
    "tenant_id",
    "correlation_id",
    "step_sequence",
    "step_kind",
    "started_at",
    "duration_ms",
    "outcome",
    "failure_cause",
    "backend_id",
    "model_id",
    "attempt_number",
    "reason",
    "candidates",
    "failing_step_sequence",
)
_META = MessageMeta(
    partition=0,
    offset=11,
    fallback_id="run-step-0-11",
    topic="onex.evt.omnimarket.delegation-run-step.v1",
)


def _step(tenant: str) -> dict[str, Any]:
    return {**_CHAIN[1], "tenant_id": tenant}


class _Transaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _Connection:
    """Rows by key, as the primary key keeps them, and the mirror's lookups."""

    def __init__(self, mirror: dict[str, str]) -> None:
        self.rows: dict[tuple[str, str, int], dict[str, Any]] = {}
        self.session_tenants: list[str] = []
        self._mirror = mirror

    def transaction(self) -> _Transaction:
        return _Transaction()

    async def execute(self, sql: str, *args: Any) -> None:
        if "set_config" in sql:
            self.session_tenants.append(str(args[1]))

    async def fetchval(self, sql: str, *args: Any) -> Any:
        if "tenant_slug = $1" in sql:
            return self._mirror.get(str(args[0]))
        if "tenant_uuid = $1" in sql:
            wanted = str(args[0])
            return wanted if wanted in self._mirror.values() else None
        # The mirror's watermark: unanswerable here, so no catch-up wait.
        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        key = (str(args[0]), str(args[1]), int(args[2]))
        if "INSERT INTO public.run_trace" in sql:
            if key in self.rows:
                return []
            row = dict(zip(_COLUMNS, args, strict=True))
            row["updated_at"] = datetime(2026, 10, 17, tzinfo=UTC)
            row["projection_cursor"] = len(self.rows) + 1
            self.rows[key] = row
            return [dict(row)]
        assert sql.lstrip().upper().startswith("SELECT"), sql
        return [dict(self.rows[key])] if key in self.rows else []


class _Acquire:
    def __init__(self, conn: _Connection) -> None:
        self._conn = conn

    async def __aenter__(self) -> _Connection:
        return self._conn

    async def __aexit__(self, *_exc: object) -> bool:
        return False


class _Pool:
    def __init__(self, conn: _Connection) -> None:
        self._conn = conn

    def acquire(self) -> _Acquire:
        return _Acquire(self._conn)


class _Db:
    def __init__(self, conn: _Connection) -> None:
        self.pool = _Pool(conn)


def _writer(
    conn: _Connection, published: list[dict[str, Any]]
) -> RunTraceProjectionWriter:
    stand_in = _Db(conn)

    class _Writer(RunTraceProjectionWriter):
        @property
        def db(self) -> Any:
            return stand_in

        async def publish_snapshot_delta(self, exposure: Any, **kwargs: Any) -> bool:
            published.append(kwargs)
            return True

    return _Writer()


async def test_a_slug_step_is_stored_and_published_under_the_mirror_uuid() -> None:
    conn = _Connection({_SLUG: _UUID})
    published: list[dict[str, Any]] = []
    row = await _writer(conn, published)._project_step(_step(_SLUG), _META)

    assert row is not None
    assert [key[0] for key in conn.rows] == [_UUID]
    assert row["tenant_id"] == _UUID
    assert [p["row"]["tenant_id"] for p in published] == [_UUID]
    assert [p["tenant_id"] for p in published] == [_UUID]
    assert _SLUG not in json.dumps([p["source_event_id"] for p in published])


async def test_the_session_tenant_is_the_row_tenant() -> None:
    conn = _Connection({_SLUG: _UUID})
    await _writer(conn, [])._project_step(_step(_SLUG), _META)
    assert conn.session_tenants == [_UUID]


async def test_an_unknown_slug_is_refused_and_writes_nothing() -> None:
    conn = _Connection({})
    published: list[dict[str, Any]] = []
    with pytest.raises(TenantRegistryResolutionError):
        await _writer(conn, published)._project_step(_step("local-unknown"), _META)
    assert conn.rows == {}
    assert published == []


async def test_a_uuid_the_mirror_holds_is_stored_unchanged() -> None:
    conn = _Connection({_SLUG: _UUID})
    row = await _writer(conn, [])._project_step(_step(_UUID), _META)
    assert row is not None
    assert row["tenant_id"] == _UUID
    assert conn.session_tenants == [_UUID]


async def test_a_uuid_the_mirror_does_not_hold_is_refused() -> None:
    conn = _Connection({_SLUG: _UUID})
    other = "11111111-2222-4333-8444-555555555555"
    with pytest.raises(TenantRegistryResolutionError):
        await _writer(conn, [])._project_step(_step(other), _META)
    assert conn.rows == {}


async def test_a_redelivered_slug_step_stores_nothing_new_and_republishes_the_stored_row() -> (
    None
):
    conn = _Connection({_SLUG: _UUID})
    published: list[dict[str, Any]] = []
    writer = _writer(conn, published)
    first = await writer._project_step(_step(_SLUG), _META)
    again = await writer._project_step(
        {**_step(_SLUG), "reason": "a different reason"}, _META
    )

    assert first is not None
    assert again is None
    assert len(conn.rows) == 1
    assert [p["row"]["tenant_id"] for p in published] == [_UUID, _UUID]
    assert published[1]["row"]["reason"] == first["reason"]
