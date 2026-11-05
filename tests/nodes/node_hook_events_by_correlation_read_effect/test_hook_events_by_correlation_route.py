# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Behaviour of GET /v1/projections/hook-events/by-correlation on the mounted app.

Ported from the hosted service's route tests: the assertions move, the file does
not. Credentialled access, the three data states, tenant scoping, payload
containment, the 503 taxonomy and the query bounds.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_hook_events_by_correlation_read_effect.handlers.http_dependencies import (
    PostgresHookEventsReader,
)
from omnimarket.nodes.node_hook_events_by_correlation_read_effect.protocols.protocol_hook_events_reader import (
    ProjectionAbsentError,
    ProjectionNotConfiguredError,
    ProjectionUnavailableError,
)
from tests.nodes.node_hook_events_by_correlation_read_effect.support import (
    CID,
    KEY_A,
    ROUTE,
    SLUG_A,
    FakeReader,
    client,
    row,
)

pytestmark = pytest.mark.unit
AUTH = {"x-api-key": KEY_A}


async def test_found_returns_the_row():
    async with client(FakeReader([row()])) as c:
        resp = await c.get(ROUTE, params={"correlation_id": CID}, headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["data_state"] == "found"
    assert body["correlation_id"] == CID
    assert body["count"] == 1
    assert body["projection"] == "hook_events"
    assert body["rows"][0]["event_type"] == "tool-executed"
    assert body["rows"][0]["occurred_at"] == "2026-08-30T15:06:24Z"
    assert body["rows"][0]["captured_at"] == "2026-08-30T15:06:25Z"
    assert body["rows"][0]["correlation_id"] == CID
    assert body["tenant_scope"] == SLUG_A


async def test_not_found_is_typed_and_200_and_names_the_scope():
    async with client(FakeReader([])) as c:
        resp = await c.get(ROUTE, params={"correlation_id": CID}, headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["data_state"] == "not_found"
    assert body["count"] == 0
    assert body["rows"] == []
    assert body["correlation_id"] == CID
    assert body["tenant_scope"] == SLUG_A


async def test_missing_projection_is_its_own_state_never_not_found():
    reader = FakeReader(raises=ProjectionAbsentError("hook_events"))
    async with client(reader) as c:
        resp = await c.get(ROUTE, params={"correlation_id": CID}, headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["data_state"] == "projection_absent"
    assert body["count"] == 0
    assert body["rows"] == []
    assert body["tenant_scope"] == SLUG_A


async def test_query_is_scoped_to_the_resolved_tenant_and_ignores_caller_tenant():
    reader = FakeReader([])
    async with client(reader) as c:
        resp = await c.get(
            ROUTE,
            params={
                "correlation_id": CID,
                "tenant_id": "attacker-tenant",
                "tenant_slug": "attacker-tenant",
            },
            headers=AUTH | {"x-tenant-id": "attacker-tenant"},
        )
    assert resp.status_code == 200
    assert resp.json()["tenant_scope"] == SLUG_A
    assert reader.calls == [{"tenant_slug": SLUG_A, "correlation_id": CID, "limit": 10}]


async def test_payload_is_withheld_by_default_and_returned_on_request():
    async with client(FakeReader([row()])) as c:
        default = (
            await c.get(ROUTE, params={"correlation_id": CID}, headers=AUTH)
        ).json()
        opted_in = (
            await c.get(
                ROUTE,
                params={"correlation_id": CID, "include_payload": "true"},
                headers=AUTH,
            )
        ).json()
    assert default["rows"][0]["payload"] is None
    assert "do-not-leak" not in str(default)
    assert opted_in["rows"][0]["payload"] == {
        "tool": "Bash",
        "secret_looking_field": "do-not-leak",
    }


@pytest.mark.parametrize(
    "exc",
    [ProjectionNotConfiguredError("boom"), ProjectionUnavailableError("boom")],
)
async def test_database_problems_are_503_never_a_fabricated_empty(exc):
    async with client(FakeReader(raises=exc)) as c:
        resp = await c.get(ROUTE, params={"correlation_id": CID}, headers=AUTH)
    assert resp.status_code == 503
    assert "data_state" not in resp.json()
    assert resp.json()["detail"].startswith("Ledger projection database is ")


async def test_correlation_id_is_required_and_bounded():
    reader = FakeReader([])
    async with client(reader) as c:
        assert (await c.get(ROUTE, headers=AUTH)).status_code == 422
        too_long = await c.get(
            ROUTE, params={"correlation_id": "x" * 200}, headers=AUTH
        )
        assert too_long.status_code == 422
        empty = await c.get(ROUTE, params={"correlation_id": ""}, headers=AUTH)
        assert empty.status_code == 422
        at_bound = await c.get(ROUTE, params={"correlation_id": "x" * 64}, headers=AUTH)
        assert at_bound.status_code == 200
    assert len(reader.calls) == 1


async def test_limit_is_bounded_and_defaults_to_ten():
    reader = FakeReader([])
    async with client(reader) as c:
        for bad in (5000, 101, 0, -1):
            resp = await c.get(
                ROUTE, params={"correlation_id": CID, "limit": bad}, headers=AUTH
            )
            assert resp.status_code == 422, bad
        ok = await c.get(
            ROUTE, params={"correlation_id": CID, "limit": 100}, headers=AUTH
        )
        assert ok.status_code == 200
    assert [c["limit"] for c in reader.calls] == [100]


async def test_rows_are_capped_by_limit():
    rows = [row(event_id=f"e{i}") for i in range(5)]
    async with client(FakeReader(rows)) as c:
        body = (
            await c.get(ROUTE, params={"correlation_id": CID, "limit": 2}, headers=AUTH)
        ).json()
    assert body["count"] == 2
    assert len(body["rows"]) == 2


async def test_only_get_is_served():
    async with client(FakeReader([])) as c:
        resp = await c.post(ROUTE, params={"correlation_id": CID}, headers=AUTH)
    assert resp.status_code == 405


class _FakeConn:
    def __init__(self, log: list[tuple[str, tuple]]) -> None:
        self.log = log

    async def execute(self, sql: str, *args):
        self.log.append((sql, args))

    async def fetch(self, sql: str, *args):
        self.log.append((sql, args))
        return [{"payload": '{"k": "v"}', "correlation_id": CID}]

    def transaction(self):
        return _Ctx(None)


class _Ctx:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *exc):
        return False


class _FakePool:
    def __init__(self, log):
        self.conn = _FakeConn(log)

    def acquire(self):
        return _Ctx(self.conn)


async def test_database_adapter_binds_tenant_predicate_and_rls_guc():
    log: list[tuple[str, tuple]] = []
    reader = PostgresHookEventsReader()
    reader._pool = _FakePool(log)
    rows = await reader.read(tenant_slug=SLUG_A, correlation_id=CID, limit=3)
    (guc_sql, guc_args), (sql, args) = log
    assert "set_config($1, $2, true)" in guc_sql
    assert guc_args == ("app.tenant_id", SLUG_A)
    assert "tenant_id = $1" in sql
    assert "correlation_id = $2" in sql
    assert "ORDER BY occurred_at DESC" in sql
    assert "SELECT *" not in sql
    assert args == (SLUG_A, CID, 3)
    assert rows[0]["payload"] == {"k": "v"}


async def test_database_adapter_refuses_blank_slug_before_any_statement():
    log: list[tuple[str, tuple]] = []
    reader = PostgresHookEventsReader()
    reader._pool = _FakePool(log)
    with pytest.raises(ProjectionUnavailableError):
        await reader.read(tenant_slug="  ", correlation_id=CID, limit=3)
    assert log == []
